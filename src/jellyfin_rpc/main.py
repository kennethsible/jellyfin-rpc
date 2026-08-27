import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import ssl
import sys
import time
import uuid
from configparser import ConfigParser, SectionProxy
from contextlib import suppress
from email.utils import parseaddr
from importlib.metadata import metadata
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from typing import Any

import aiohttp
import certifi
from aiohttp import ClientSession, WSMsgType
from aiohttp_client_cache import CacheBackend
from aiohttp_client_cache.session import CachedSession
from langcodes import Language
from pypresence.exceptions import PyPresenceException
from pypresence.presence import AioPresence
from pypresence.types import ActivityType, StatusDisplayType

CLIENT_ID = '1238889120672120853'
logger = logging.getLogger('RPC')

pkg_metadata = metadata('jellyfin-rpc')
contact_info = parseaddr(pkg_metadata['Author-email'])[1]
RPC_VERSION = pkg_metadata['Version']
USER_AGENT = f'Jellyfin-RPC/{RPC_VERSION} ( {contact_info} )'


def load_config(ini_path: str) -> SectionProxy:
    config = ConfigParser()
    config.read(ini_path)
    if config.get('DEFAULT', 'API_TOKEN', fallback=None):
        jf_api_key = config.get('DEFAULT', 'API_TOKEN')
        config.set('DEFAULT', 'JELLYFIN_API_KEY', jf_api_key)
    if config.get('DEFAULT', 'USERNAME', fallback=None):
        jf_username = config.get('DEFAULT', 'USERNAME')
        config.set('DEFAULT', 'JELLYFIN_USERNAME', jf_username)
    return config['DEFAULT']


def save_config(config_parser: ConfigParser, ini_path: str) -> None:
    with open(ini_path, 'w') as ini_file:
        config_parser.write(ini_file)


def parse_delimited_list(config: SectionProxy, option: str) -> list[str]:
    option_split = re.split(r'[,;|]', config.get(option, ''))
    return [x.strip() for x in option_split if x.strip()]


def get_valid_level(level_str: str, default: int) -> int:
    level_mapping = logging.getLevelNamesMapping()
    return level_mapping.get(level_str.upper().strip(), default)


def get_lang_code(lang_str: str) -> str | None:
    lang_str = lang_str.strip()
    try:
        lang = Language.get(lang_str)
        if lang.language:
            return lang.language
    except (ImportError, LookupError, ValueError):
        pass
    try:
        return Language.find(lang_str).language
    except (ImportError, LookupError, ValueError):
        return None


def get_device_id(config: SectionProxy) -> str:
    if device_id := config.get('JELLYFIN_DEVICE_ID'):
        return device_id
    try:
        hardware_str = f'Jellyfin-RPC-{uuid.getnode()}-{platform.node()}'
        device_id = hashlib.sha256(hardware_str.encode('utf-8')).hexdigest()[:32]
    except (OSError, AttributeError):
        device_id = f'Jellyfin-RPC-Fallback-{uuid.uuid4().hex[:16]}'
    config['JELLYFIN_DEVICE_ID'] = device_id
    return device_id


def build_auth_header(device_id: str, api_key: str | None = None) -> str:
    client, device = 'Jellyfin RPC', 'Discord RPC'
    base_auth = f'MediaBrowser Client="{client}", Device="{device}", DeviceId="{device_id}", Version="{RPC_VERSION}"'
    if api_key:
        base_auth += f', Token="{api_key}"'
    return base_auth


async def initiate_quick_connect(
    session: ClientSession, jf_host: str, device_id: str
) -> tuple[str, str]:
    headers = {'Accept': 'application/json', 'Authorization': build_auth_header(device_id)}
    try:
        async with session.post(f'{jf_host}/QuickConnect/Initiate', headers=headers) as response:
            response.raise_for_status()
            init_data = await response.json()
            secret = init_data['Secret']
            code = init_data['Code']
            logger.info(f'Quick Connect Code: {code}')
    except (TimeoutError, aiohttp.ClientError, JSONDecodeError, KeyError) as e:
        logger.error(f'Failed to Initiate Quick Connect: {e}')
        sys.exit(1)

    while True:
        try:
            async with session.get(
                f'{jf_host}/QuickConnect/Connect?secret={secret}', headers=headers
            ) as response:
                if response.status == 200:
                    connect_data = await response.json()
                    if connect_data.get('Authenticated') is True:
                        break
        except (TimeoutError, aiohttp.ClientError, JSONDecodeError, KeyError):
            pass
        await asyncio.sleep(5)

    try:
        payload = {'Secret': secret}
        async with session.post(
            f'{jf_host}/Users/AuthenticateWithQuickConnect', headers=headers, json=payload
        ) as response:
            response.raise_for_status()
            auth_data = await response.json()
            token = auth_data['AccessToken']
            username = auth_data['User']['Name']
            logger.info(f'Successfully Authenticated via Quick Connect ({username})')
            return token, username
    except (TimeoutError, aiohttp.ClientError, JSONDecodeError, KeyError) as e:
        logger.error(f'Failed to Retrieve User Access Token: {e}')
        sys.exit(1)


async def get_jf_user_and_server(
    session: ClientSession,
    config: SectionProxy,
    ini_path: str,
    show_server_name: bool,
    polling_rate: int,
) -> tuple[str, str | None]:
    try:
        jf_host = config['JELLYFIN_HOST'].rstrip('/')
        jf_username = config['JELLYFIN_USERNAME']
        jf_api_key = config['JELLYFIN_API_KEY']
    except KeyError as e:
        logger.error(f'Missing Key in INI Config: {e}')
        sys.exit(1)

    device_id = get_device_id(config)
    if not jf_api_key:
        jf_api_key, jf_username = await initiate_quick_connect(session, jf_host, device_id)

        config['JELLYFIN_API_KEY'] = jf_api_key
        config['JELLYFIN_USERNAME'] = jf_username

        config_parser = ConfigParser()
        config_parser.read(ini_path)
        config_parser.set('DEFAULT', 'JELLYFIN_API_KEY', jf_api_key)
        config_parser.set('DEFAULT', 'JELLYFIN_USERNAME', jf_username)

        await asyncio.to_thread(save_config, config_parser, ini_path)

    initial_attempt = True
    headers = {
        'Accept': 'application/json',
        'Authorization': build_auth_header(device_id, jf_api_key),
    }

    while True:
        try:
            async with session.get(f'{jf_host}/Users', headers=headers) as response:
                response.raise_for_status()
                users_data = await response.json()

            user_id = None
            for user in users_data:
                if jf_username == user.get('Name', ''):
                    user_id = user.get('Id')
            if user_id is None:
                logger.error(f'Jellyfin User Not Found: {jf_username}')
                sys.exit(1)

            server_name = None
            if show_server_name:
                async with session.get(f'{jf_host}/System/Info', headers=headers) as response:
                    response.raise_for_status()
                    system_info = await response.json()
                    server_name = system_info.get('ServerName', 'Jellyfin')

            logger.info('Connected to Jellyfin Server')
            return user_id, server_name

        except (TimeoutError, aiohttp.ClientError) as e:
            if initial_attempt:
                logger.error(f'Jellyfin API Network Error ({type(e).__name__}). Retrying...')
                logger.debug(e)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue
        except (ValueError, KeyError) as e:
            if initial_attempt:
                logger.error(f'Jellyfin API Parsing Error ({type(e).__name__}). Retrying...')
                logger.debug(e)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue


async def check_tmdb_connection(session: ClientSession, api_key: str) -> None:
    config_url = 'https://api.themoviedb.org/3/configuration'
    config_params = {'api_key': api_key}
    try:
        async with session.get(config_url, params=config_params) as response:
            response.raise_for_status()
        logger.info('Connected to TMDB API')
    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)


async def get_series_id(
    session: ClientSession, api_key: str, title: str, year: int | None = None
) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/tv'
    search_params = {'api_key': api_key, 'query': title}
    if year is not None:
        search_params['first_air_date_year'] = str(year)
    try:
        async with session.get(search_url, params=search_params) as response:
            response.raise_for_status()
            data = await response.json()
            if results := data.get('results'):
                return results[0].get('id')
    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError) as e:
        logger.warning(f'TMDB API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return None


async def get_movie_id(
    session: ClientSession, api_key: str, title: str, year: int | None = None
) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/movie'
    search_params = {'api_key': api_key, 'query': title}
    if year is not None:
        search_params['first_air_date_year'] = str(year)
    try:
        async with session.get(search_url, params=search_params) as response:
            response.raise_for_status()
            data = await response.json()
            if results := data.get('results'):
                return results[0].get('id')
    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError) as e:
        logger.warning(f'TMDB API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return None


async def get_music_id(session: ClientSession, artist: str, album: str) -> str | None:
    artist, album = artist.lower(), album.lower()
    search_url = 'https://musicbrainz.org/ws/2/release-group'
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    artist_query = f'artist:({artist}) OR artistalias:({artist})'
    album_query = f'releasegroup:({album}) OR alias:({album}'
    params = {'query': f'({artist_query}) AND ({album_query})', 'fmt': 'json'}
    try:
        async with session.get(search_url, headers=headers, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            return data['release-groups'][0]['id']
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f'MusicBrainz API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'MusicBrainz API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return None


def select_poster(posters: list[dict[str, Any]], languages: list[str]) -> dict[str, Any] | None:
    if not posters:
        return None

    def get_poster_score(poster: dict[str, Any]) -> tuple[float, int, int]:
        return (
            poster.get('vote_average', 0.0),
            poster.get('vote_count', 0),
            poster.get('width', 0),
        )

    posters_by_lang = {}
    for poster in posters:
        lang_code = poster.get('iso_639_1') or None
        if lang_code not in posters_by_lang:
            posters_by_lang[lang_code] = []
        posters_by_lang[lang_code].append(poster)

    for lang_code in languages:
        target_lang = lang_code or None
        if target_lang in posters_by_lang:
            return max(posters_by_lang[target_lang], key=get_poster_score)
    return max(posters, key=get_poster_score)


async def get_series_poster(
    session: ClientSession, api_key: str, tmdb_id: str, languages: list[str]
) -> str:
    try:
        if languages:
            images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/images'
            async with session.get(images_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
                logger.warning('No Poster Available on TMDB. Skipping...')
        else:
            series_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}'
            async with session.get(series_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return 'https://image.tmdb.org/t/p/w185/' + poster_path
                logger.warning('No Poster Available on TMDB. Skipping...')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'TMDB API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return 'large_image'


async def get_season_poster(
    session: ClientSession,
    api_key: str,
    tmdb_id: str,
    languages: list[str],
    season: int | None = None,
) -> str:
    if season is None:
        return await get_series_poster(session, api_key, tmdb_id, languages)

    try:
        if languages:
            images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images'
            async with session.get(images_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
                logger.warning('No Poster Available on TMDB. Skipping...')
        else:
            season_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}'
            async with session.get(season_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return 'https://image.tmdb.org/t/p/w185/' + poster_path
                logger.warning('No Poster Available on TMDB. Skipping...')
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError):
        pass

    return await get_series_poster(session, api_key, tmdb_id, languages)


async def get_movie_poster(
    session: ClientSession, api_key: str, tmdb_id: str, languages: list[str]
) -> str:
    try:
        if languages:
            images_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}/images'
            async with session.get(images_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
                logger.warning('No Poster Available on TMDB. Skipping...')
        else:
            movie_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}'
            async with session.get(movie_url, params={'api_key': api_key}) as response:
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return 'https://image.tmdb.org/t/p/w185/' + poster_path
                logger.warning('No Poster Available on TMDB. Skipping...')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'TMDB API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return 'large_image'


async def get_release_group_cover(session: ClientSession, group_id: str) -> str:
    try:
        async with session.get(f'https://coverartarchive.org/release-group/{group_id}') as response:
            response.raise_for_status()
            data = await response.json()
            if 'images' not in data:
                logger.warning('No Cover Art Available on Cover Art Archive. Skipping...')
            return data['images'][0]['image']
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f'Cover Art Archive API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'Cover Art Archive API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return 'large_image'


async def get_release_cover(
    session: ClientSession, group_id: str, release_id: str | None = None
) -> str:
    if not release_id:
        return await get_release_group_cover(session, group_id)
    try:
        async with session.get(f'https://coverartarchive.org/release/{release_id}') as response:
            response.raise_for_status()
            data = await response.json()
            return data['images'][0]['image']
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError):
        return await get_release_group_cover(session, group_id)


async def await_connection(discord_rpc: AioPresence, polling_rate: int) -> None:
    initial_attempt = True
    while True:
        try:
            await discord_rpc.connect()
            logger.info('Connected to Discord Client')
        except (PyPresenceException, OSError) as e:
            if initial_attempt:
                logger.error(f'Discord Client Connection Failed ({type(e).__name__}). Retrying...')
                logger.debug(e)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue
        break


async def ws_listener(
    session: ClientSession,
    config: SectionProxy,
    polling_rate: int,
    ws_state: dict[str, Any],
    wake_event: asyncio.Event,
) -> None:
    jf_host = config['JELLYFIN_HOST'].rstrip('/')
    device_id = get_device_id(config)
    ws_protocol = 'wss://' if jf_host.startswith('https://') else 'ws://'
    ws_host = jf_host.split('://', 1)[-1]

    initial_attempt = True
    while True:
        jf_api_key = config.get('JELLYFIN_API_KEY', '')
        if not jf_api_key:
            await asyncio.sleep(1)
            continue

        ws_url = f'{ws_protocol}{ws_host}/socket?api_key={jf_api_key}&deviceId={device_id}'
        try:
            async with session.ws_connect(ws_url, heartbeat=30.0) as ws:
                ws_state['ws_connected'] = True
                initial_attempt = True
                # logger.info('Connected to Jellyfin WebSocket')
                await ws.send_str(json.dumps({'MessageType': 'SessionsStart', 'Data': '0,1500'}))
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        if payload.get('MessageType') == 'Sessions':
                            ws_state['sessions'] = payload.get('Data', [])
                            wake_event.set()
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
        except (aiohttp.ClientError, TimeoutError, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                break
            if initial_attempt:
                logger.warning(f'Jellyfin WebSocket Error ({type(e).__name__}). Skipping...')
                logger.debug(e)
                initial_attempt = False
        finally:
            ws_state['ws_connected'] = False
        await asyncio.sleep(polling_rate)


async def activity_loop(
    jf_session: ClientSession,
    cache_session: ClientSession,
    discord_rpc: AioPresence,
    config: SectionProxy,
    ini_path: str,
    polling_rate: int,
    seek_threshold: int,
    ws_state: dict[str, Any],
    wake_event: asyncio.Event,
) -> None:
    jf_host = config['JELLYFIN_HOST'].rstrip('/')
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_server_name = config.getboolean('SHOW_SERVER_NAME', False)
    show_jf_logo = config.getboolean('SHOW_JELLYFIN_LOGO') or config.getboolean(
        'SHOW_JELLYFIN_ICON', False
    )

    user_id, server_name = await get_jf_user_and_server(
        jf_session, config, ini_path, show_server_name, polling_rate
    )
    jf_username = config['JELLYFIN_USERNAME']
    jf_api_key = config['JELLYFIN_API_KEY']
    jf_headers = {
        'Accept': 'application/json',
        'Authorization': build_auth_header(get_device_id(config), jf_api_key),
    }

    if tmdb_api_key := config.get('TMDB_API_KEY'):
        await check_tmdb_connection(cache_session, tmdb_api_key)

    languages = parse_delimited_list(config, 'POSTER_LANGUAGES')
    for i, lang in enumerate(languages):
        lang_code = get_lang_code(lang) or lang
        if lang_code != lang:
            languages[i] = lang_code
        if len(lang_code) != 2 or not lang_code.isalpha():
            logger.warning(f'Invalid ISO 639-1 Language "{lang_code}"')
    if config.getboolean('TEXTLESS_POSTERS', False):
        languages.insert(0, '')

    always_use_tmdb = config.getboolean('ALWAYS_USE_TMDB', False)
    if always_use_tmdb and not tmdb_api_key:
        logger.warning('Missing TMDB API Key')
    season_over_series = config.getboolean('SEASON_OVER_SERIES', False)

    always_use_musicbrainz = config.getboolean('ALWAYS_USE_MUSICBRAINZ', False)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', False)

    filter_mode = config.get('FILTER_MODE', 'BLACKLIST').upper()
    filter_libraries = parse_delimited_list(config, 'FILTER_LIBRARIES')

    media_types = parse_delimited_list(config, 'MEDIA_TYPES')
    jf_media_types = set()
    if 'Shows' in media_types:
        jf_media_types.add('Episode')
    if 'Movies' in media_types:
        jf_media_types.add('Movie')
    if 'Music' in media_types:
        jf_media_types.add('Audio')

    activity = previous_activity = None
    previous_warning = False  # Suppresses Duplicate Warnings
    previous_playstate = False  # Last Pause State (True=Paused)
    previous_playback = None  # Playback Time of Last Media Position
    previous_timestamp = None  # System Time of Last Media Position
    previous_update = 0.0  # System Time of Last Activity Update
    pending_update = False  # Tracks Deferred Update During Cooldown
    pending_payload = None  # Event Payload for Deferred Update

    cached_item_id = cached_library = None
    cached_kwargs: dict[str, Any] = {}

    while True:
        if ws_state.get('ws_connected'):
            try:
                if pending_update:
                    remaining_cooldown = polling_rate - (time.time() - previous_update)
                    wait_timeout = max(0.05, remaining_cooldown)
                else:
                    wait_timeout = polling_rate
                await asyncio.wait_for(wake_event.wait(), timeout=wait_timeout)
                wake_event.clear()
            except TimeoutError:
                pass
            sessions = ws_state.get('sessions', [])
        else:
            try:
                async with jf_session.get(f'{jf_host}/Sessions', headers=jf_headers) as response:
                    response.raise_for_status()
                    sessions = await response.json()
            except (aiohttp.ClientError, TimeoutError) as e:
                logger.error(f'Session Polling Error: {type(e).__name__}')
                logger.debug(e)
                user_id, server_name = await get_jf_user_and_server(
                    jf_session, config, ini_path, show_server_name, polling_rate
                )
                jf_username = config['JELLYFIN_USERNAME']
                jf_api_key = config['JELLYFIN_API_KEY']
                jf_headers['Authorization'] = build_auth_header(get_device_id(config), jf_api_key)
                await asyncio.sleep(polling_rate)
                continue
            except ValueError as e:
                logger.error(f'Session Parsing Error: {type(e).__name__}')
                logger.debug(e)
                await asyncio.sleep(polling_rate)
                continue

        session_data: dict[str, Any] = {}
        for session in sessions:
            if session.get('UserName') != jf_username:
                continue
            if not (item := session.get('NowPlayingItem')):
                continue
            media_type = item.get('Type')
            if media_type in jf_media_types:
                session_data = session
                break

        if 'NowPlayingItem' in session_data:
            try:
                session_paused = session_data['PlayState']['IsPaused']
            except KeyError as e:
                logger.warning(f'Missing Key in Session Data: {e}')
                session_paused = False

            if session_paused and not show_when_paused:
                if previous_activity is not None:
                    try:
                        await discord_rpc.clear()
                        logger.info('Activity Cleared')
                    except (PyPresenceException, OSError, KeyError) as e:
                        logger.error(f'Failed to Clear Activity: {type(e).__name__}')
                        logger.debug(e)
                        await await_connection(discord_rpc, polling_rate)
                        await asyncio.sleep(polling_rate)
                        continue

                    previous_activity = None
                    previous_playstate = False
                    previous_playback = None
                    previous_timestamp = None
                    previous_update = time.time()
                    pending_update = False
                continue

            try:
                state = details = None
                media_dict = session_data['NowPlayingItem']
                item_id = media_dict.get('Id')

                library_id = None
                if item_id == cached_item_id:
                    library_id = cached_library
                elif item_id:
                    try:
                        ancestors_url = f'{jf_host}/Items/{item_id}/Ancestors'
                        async with jf_session.get(
                            ancestors_url, headers=jf_headers, params={'userId': user_id}
                        ) as response:
                            response.raise_for_status()
                            ancestors = await response.json()
                        for ancestor in ancestors:
                            if ancestor.get('Type') in ('CollectionFolder', 'AggregateFolder'):
                                library_id = ancestor.get('Id')
                                break
                        if library_id:
                            cached_item_id, cached_library = item_id, library_id
                    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
                        logger.error(f'Library Retrieval Failed ({type(e).__name__}). Skipping...')
                        logger.debug(e)

                match filter_mode:
                    case 'WHITELIST':
                        is_allowed = bool(library_id and library_id in filter_libraries)
                    case 'BLACKLIST':
                        is_allowed = not (library_id and library_id in filter_libraries)
                    case _:
                        is_allowed = True

                if not is_allowed:
                    if previous_activity is not None:
                        try:
                            await discord_rpc.clear()
                            logger.info('Activity Cleared (Library Blocked)')
                        except (PyPresenceException, OSError, KeyError) as e:
                            logger.error(f'Failed to Clear Activity: {type(e).__name__}')
                            logger.debug(e)
                            await await_connection(discord_rpc, polling_rate)
                            await asyncio.sleep(polling_rate)
                            continue

                        previous_activity = None
                        previous_playstate = False
                        previous_playback = None
                        previous_timestamp = None
                        previous_update = time.time()
                        pending_update = False
                    continue

                match media_type := media_dict['Type']:
                    case 'Episode':
                        activity_type = ActivityType.WATCHING
                        season = media_dict['ParentIndexNumber']
                        episode = media_dict['IndexNumber']
                        details = media_dict['SeriesName']
                        state = f'{f"S{season}:E{episode}"} - {media_dict["Name"]}'
                        activity = f'{details} {state.split(" - ")[0]}'
                    case 'Movie':
                        activity_type = ActivityType.WATCHING
                        details = media_dict['Name']
                        activity = details
                    case 'Audio':
                        activity_type = ActivityType.LISTENING
                        if media_dict.get('Artists'):
                            state = ', '.join(media_dict['Artists'])
                        if media_dict.get('Album'):
                            if state:
                                state += f' - {media_dict["Album"]}'
                            else:
                                state = media_dict['Album']
                        details = media_dict['Name']
                        activity = str(details)
                        if state:
                            activity += f' - {state.split(" - ")[0]}'
                    case _:
                        if not previous_warning:
                            logger.warning(f'Unsupported Media Type "{media_type}". Skipping...')
                            previous_warning = True
                        if previous_activity is not None:
                            try:
                                await discord_rpc.clear()
                                logger.info('Activity Cleared (Unsupported Media)')
                            except (PyPresenceException, OSError, KeyError) as e:
                                logger.error(f'Failed to Clear Activity: {type(e).__name__}')
                                logger.debug(e)
                                await await_connection(discord_rpc, polling_rate)
                                await asyncio.sleep(polling_rate)
                                continue

                            previous_activity = None
                            previous_playstate = False
                            previous_playback = None
                            previous_timestamp = None
                            previous_update = time.time()
                            pending_update = False
                        continue  # raise NotImplementedError()

                if len(details) < 2:  # e.g., Chinese characters
                    details += ' '
            except KeyError as e:
                if not previous_warning:
                    logger.warning(f'Missing Key in Session Data: {e}. Skipping...')
                    previous_warning = True
                await asyncio.sleep(polling_rate)
                continue
            previous_warning = False

            current_playback = None
            current_start = current_end = None
            try:
                position_ticks = int(session_data['PlayState']['PositionTicks'])
                current_playback = position_ticks / 10_000_000
                if not session_paused:
                    current_start = int(time.time() - current_playback)
                    runtime_ticks = int(media_dict['RunTimeTicks'])
                    current_end = int(current_start + runtime_ticks / 10_000_000)
            except (KeyError, TypeError, ValueError):
                pass

            STALE_GRACE_PERIOD = 5
            if current_end and time.time() >= (current_end + STALE_GRACE_PERIOD):
                if previous_activity is not None:
                    try:
                        await discord_rpc.clear()
                        logger.info('Activity Cleared (Stale Session)')
                    except (PyPresenceException, OSError, KeyError) as e:
                        logger.error(f'Failed to Clear Activity: {type(e).__name__}')
                        logger.debug(e)
                        await await_connection(discord_rpc, polling_rate)
                        await asyncio.sleep(polling_rate)
                        continue

                    previous_activity = None
                    previous_playstate = False
                    previous_playback = None
                    previous_timestamp = None
                    previous_update = time.time()
                    pending_update = False
                continue

            media_changed = previous_activity != activity
            playstate_changed = previous_playstate != session_paused

            seek_detected = False
            current_timestamp = time.time()
            if (
                not media_changed
                and not session_paused
                and not previous_playstate
                and current_playback is not None
                and previous_playback is not None
                and previous_timestamp is not None
            ):
                timestamp_elapsed = current_timestamp - previous_timestamp
                expected_playback = previous_playback + timestamp_elapsed
                if abs(current_playback - expected_playback) > seek_threshold:
                    seek_detected = True

            previous_playback = current_playback
            previous_timestamp = current_timestamp

            if media_changed:
                poster_url = 'large_image'
                state_url = large_url = details_url = None
                is_https = jf_host.startswith('https://')

                if media_type == 'Episode':
                    tmdb_id = series_year = None
                    if jf_series_id := media_dict.get('SeriesId'):
                        try:
                            async with jf_session.get(
                                f'{jf_host}/Items/{jf_series_id}',
                                headers=jf_headers,
                                params={'userId': user_id},
                            ) as response:
                                response.raise_for_status()
                                series_item = await response.json()
                                series_year = series_item.get('ProductionYear')
                                series_ids = series_item.get('ProviderIds', {})
                                tmdb_id = series_ids.get('Tmdb') or series_ids.get('TheMovieDb')
                        except (aiohttp.ClientError, TimeoutError, ValueError):
                            pass

                    if not tmdb_id and tmdb_api_key:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'SeriesName' in media_dict:
                            tmdb_id = await get_series_id(
                                cache_session, tmdb_api_key, media_dict['SeriesName'], series_year
                            )

                    if not always_use_tmdb:
                        jf_season_id = media_dict.get('SeasonId')
                        jf_season_poster = (
                            f'{jf_host}/Items/{jf_season_id}/Images/Primary'
                            if (jf_season_id and is_https)
                            else None
                        )
                        jf_series_poster = (
                            f'{jf_host}/Items/{jf_series_id}/Images/Primary'
                            if (jf_series_id and is_https)
                            else None
                        )
                        if season_over_series and jf_season_poster:
                            poster_url = jf_season_poster
                        elif jf_series_poster:
                            poster_url = jf_series_poster
                        elif tmdb_api_key and tmdb_id:
                            season = media_dict['ParentIndexNumber']
                            if season_over_series:
                                poster_url = await get_season_poster(
                                    cache_session, tmdb_api_key, tmdb_id, languages, season
                                )
                            else:
                                poster_url = await get_series_poster(
                                    cache_session, tmdb_api_key, tmdb_id, languages
                                )
                    elif tmdb_api_key and tmdb_id:
                        season = media_dict['ParentIndexNumber']
                        if season_over_series:
                            poster_url = await get_season_poster(
                                cache_session, tmdb_api_key, tmdb_id, languages, season
                            )
                        else:
                            poster_url = await get_series_poster(
                                cache_session, tmdb_api_key, tmdb_id, languages
                            )

                    if tmdb_id:
                        details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                        if 'ParentIndexNumber' in media_dict:
                            season = media_dict['ParentIndexNumber']
                            large_url = f'{details_url}/season/{season}'
                            if 'IndexNumber' in media_dict:
                                episode = media_dict['IndexNumber']
                                state_url = f'{details_url}/season/{season}/episode/{episode}'

                elif media_type == 'Movie':
                    movie_ids = media_dict.get('ProviderIds', {})
                    movie_year = media_dict.get('ProductionYear')
                    tmdb_id = movie_ids.get('Tmdb') or movie_ids.get('TheMovieDb')

                    if not tmdb_id and tmdb_api_key:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'Name' in media_dict:
                            tmdb_id = await get_movie_id(
                                cache_session, tmdb_api_key, media_dict['Name'], movie_year
                            )

                    jf_movie_poster = (
                        f'{jf_host}/Items/{item_id}/Images/Primary'
                        if (item_id and is_https)
                        else None
                    )
                    if not always_use_tmdb and jf_movie_poster:
                        poster_url = jf_movie_poster
                    elif tmdb_api_key and tmdb_id:
                        poster_url = await get_movie_poster(
                            cache_session, tmdb_api_key, tmdb_id, languages
                        )

                    if tmdb_id:
                        details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'
                        large_url = details_url

                elif media_type == 'Audio':
                    music_ids = media_dict.get('ProviderIds', {})
                    group_id = music_ids.get('MusicBrainzReleaseGroup')
                    album_id = media_dict.get('AlbumId')

                    album_item = None
                    if not group_id and album_id:
                        try:
                            async with jf_session.get(
                                f'{jf_host}/Items/{album_id}',
                                headers=jf_headers,
                                params={'userId': user_id},
                            ) as response:
                                response.raise_for_status()
                                album_item = await response.json()
                                album_music_ids = album_item.get('ProviderIds', {})
                                group_id = album_music_ids.get('MusicBrainzReleaseGroup')
                        except (aiohttp.ClientError, TimeoutError, ValueError):
                            pass

                    if not group_id:
                        logger.warning('No MusicBrainz ID Found. Searching...')
                        if 'AlbumArtist' in media_dict and 'Album' in media_dict:
                            group_id = await get_music_id(
                                cache_session, media_dict['AlbumArtist'], media_dict['Album']
                            )

                    release_id = None
                    jf_album_cover = (
                        f'{jf_host}/Items/{album_id}/Images/Primary'
                        if (album_id and is_https)
                        else None
                    )
                    if not always_use_musicbrainz and jf_album_cover:
                        poster_url = jf_album_cover
                    elif group_id:
                        if release_over_group:
                            release_id = music_ids.get('MusicBrainzAlbum')
                            if not release_id and album_id:
                                try:
                                    if album_item is None:
                                        async with jf_session.get(
                                            f'{jf_host}/Items/{album_id}',
                                            headers=jf_headers,
                                            params={'userId': user_id},
                                        ) as response:
                                            response.raise_for_status()
                                            album_item = await response.json()
                                    album_music_ids = album_item.get('ProviderIds', {})
                                    release_id = album_music_ids.get('MusicBrainzAlbum')
                                except (aiohttp.ClientError, TimeoutError, ValueError):
                                    pass
                        poster_url = await get_release_cover(cache_session, group_id, release_id)

                    if group_id:
                        if 'MusicBrainzTrack' in music_ids:
                            track_id = music_ids['MusicBrainzTrack']
                            details_url = f'https://musicbrainz.org/track/{track_id}'
                        state_url = f'https://musicbrainz.org/release-group/{group_id}'
                        if release_id:
                            large_url = f'https://musicbrainz.org/release/{release_id}'
                        else:
                            large_url = state_url

                cached_kwargs = {
                    'activity_type': activity_type,
                    'status_display_type': StatusDisplayType.DETAILS,
                    'state': state[:128] if state else None,
                    'state_url': state_url,
                    'details': details[:128] if details else None,
                    'details_url': details_url,
                    'name': server_name,
                    'large_image': poster_url,
                    'large_url': large_url,
                }

            if media_changed or seek_detected or playstate_changed:
                pending_update = True
                if media_changed:
                    pending_payload = ('media_changed', activity)
                elif seek_detected:
                    pending_payload = ('seek_detected', None)
                elif playstate_changed:
                    pending_payload = ('playstate_changed', session_paused)

            if pending_update and (time.time() - previous_update) >= polling_rate:
                small_image = None
                if show_jf_logo:
                    small_image = 'media_paused' if session_paused else 'small_image'

                if pending_payload:
                    update_type, payload = pending_payload
                    match update_type:
                        case 'media_changed':
                            logger.info(f'"{payload}"')
                        case 'seek_detected':
                            logger.debug('Seek Detected')
                        case 'playstate_changed':
                            playstate = 'Paused' if payload else 'Resumed'
                            logger.debug(f'PlayState {playstate}')
                    pending_payload = None

                try:
                    await discord_rpc.update(
                        **cached_kwargs,
                        start=current_start,
                        end=current_end,
                        small_image=small_image,
                    )
                    previous_update = time.time()
                    previous_activity = activity
                    previous_playstate = session_paused
                    pending_update = False
                except (PyPresenceException, OSError, KeyError) as e:
                    logger.error(f'RPC Update Error: {type(e).__name__}')
                    logger.debug(e)
                    await await_connection(discord_rpc, polling_rate)
                    await asyncio.sleep(polling_rate)
                    continue

        elif previous_activity is not None:
            try:
                await discord_rpc.clear()
                logger.info('Activity Cleared')
            except (PyPresenceException, OSError, KeyError) as e:
                logger.error(f'Failed to Clear Activity: {type(e).__name__}')
                logger.debug(e)
                await await_connection(discord_rpc, polling_rate)
                await asyncio.sleep(polling_rate)
                continue

            previous_activity = None
            previous_playstate = False
            previous_playback = None
            previous_timestamp = None
            previous_update = time.time()
            pending_update = False


async def monitor_activity(
    config: SectionProxy, init_path: str, polling_rate: int, seek_threshold: int
) -> None:
    client_id = config.get('DISCORD_CLIENT_ID', CLIENT_ID)
    discord_rpc = AioPresence(client_id)
    await await_connection(discord_rpc, polling_rate)

    timeout = aiohttp.ClientTimeout(5.0)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    jf_connector = aiohttp.TCPConnector(ssl=ssl_context)
    cache_connector = aiohttp.TCPConnector(ssl=ssl_context)

    ws_state = {'sessions': [], 'ws_connected': False}
    wake_event = asyncio.Event()

    try:
        async with (
            ClientSession(connector=jf_connector, timeout=timeout) as jf_session,
            CachedSession(
                cache=CacheBackend(), connector=cache_connector, timeout=timeout
            ) as cache_session,
        ):
            ws_task = asyncio.create_task(
                ws_listener(jf_session, config, polling_rate, ws_state, wake_event)
            )
            try:
                await activity_loop(
                    jf_session,
                    cache_session,
                    discord_rpc,
                    config,
                    init_path,
                    polling_rate,
                    seek_threshold,
                    ws_state,
                    wake_event,
                )
            finally:
                ws_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ws_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        with suppress(PyPresenceException, OSError, RuntimeError):
            discord_rpc.close()


def start_discord_rpc(
    ini_path: str, log_path: str | None = None, log_queue: Queue[LogRecord] | None = None
) -> None:
    config = load_config(ini_path)
    polling_rate = max(1, config.getint('POLLING_RATE') or config.getint('REFRESH_RATE', 5))
    seek_threshold = max(1, config.getint('SEEK_THRESHOLD', 10))

    logger.setLevel(logging.DEBUG)
    log_level = get_valid_level(config.get('LOG_LEVEL', ''), logging.INFO)
    file_hdlr_level = get_valid_level(config.get('FILE_HDLR_LEVEL', ''), logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    if log_path is not None:
        max_bytes = int(config.get('LOG_MAX_BYTES', 5242880))
        max_files = int(config.get('LOG_MAX_FILES', 3))
        file_hdlr = handlers.RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=max_files, encoding='utf-8'
        )
        file_hdlr.setFormatter(formatter)
        file_hdlr.setLevel(file_hdlr_level)
        logger.addHandler(file_hdlr)

    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    stream_hdlr.setLevel(log_level)
    logger.addHandler(stream_hdlr)

    if log_queue is not None:
        queue_hdlr = handlers.QueueHandler(log_queue)
        queue_hdlr.setLevel(log_level)
        logger.addHandler(queue_hdlr)

    asyncio.run(monitor_activity(config, ini_path, polling_rate, seek_threshold))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-path', type=str)
    parser.add_argument('--log-path', type=str)
    args = parser.parse_args()

    ini_path, log_path = args.ini_path, args.log_path
    if ini_path is None or log_path is None:
        if sys.platform == 'win32':
            root_dir = os.getenv('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
            data_dir = os.path.join(root_dir, 'Jellyfin RPC')
        elif sys.platform == 'darwin':
            root_dir = os.path.expanduser('~/Library/Application Support')
            data_dir = os.path.join(root_dir, 'Jellyfin RPC')
        else:
            root_dir = os.getenv('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
            data_dir = os.path.join(root_dir, 'jellyfin-rpc')

        if ini_path is None:
            ini_path = os.path.join(data_dir, 'jellyfin_rpc.ini')
        if log_path is None:
            log_path = os.path.join(data_dir, 'jellyfin_rpc.log')

    start_discord_rpc(ini_path, log_path)


if __name__ == '__main__':
    main()
