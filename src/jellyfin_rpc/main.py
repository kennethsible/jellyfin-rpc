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
from dataclasses import dataclass
from email.utils import parseaddr
from importlib.metadata import metadata
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from typing import Any
from urllib.parse import urlparse

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
logging.addLevelName(15, 'VERBOSE')

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


async def resolve_jf_host(jf_host: str, session: aiohttp.ClientSession) -> str:
    for protocol in ('https://', 'http://'):
        candidate_url = f'{protocol}{jf_host}'
        try:
            async with session.get(
                f'{candidate_url}/System/Info/Public', timeout=aiohttp.ClientTimeout(total=5.0)
            ) as response:
                if response.status in (200, 401, 403):
                    return candidate_url
        except (aiohttp.ClientError, TimeoutError, OSError):
            continue
    return f'http://{jf_host}'


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


async def check_tmdb_auth(session: ClientSession, api_key: str) -> None:
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
        search_params['primary_release_year'] = str(year)
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


async def get_music_id_from_search(session: ClientSession, artist: str, album: str) -> str | None:
    artist, album = artist.lower(), album.lower()
    search_url = 'https://musicbrainz.org/ws/2/release-group'
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    artist_query = f'artist:({artist}) OR artistalias:({artist})'
    album_query = f'releasegroup:({album}) OR alias:({album})'
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


async def get_music_id_from_release(session: ClientSession, release_id: str) -> str | None:
    lookup_url = f'https://musicbrainz.org/ws/2/release/{release_id}'
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    params = {'inc': 'release-groups', 'fmt': 'json'}
    try:
        async with session.get(lookup_url, headers=headers, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            return data['release-group']['id']
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


def resolve_series_provider_urls(
    series_external_urls: list[dict[str, str]],
    episode_external_urls: list[dict[str, str]],
    season: int | None = None,
    episode: int | None = None,
    use_imdb: bool = False,
) -> tuple[str | None, str | None]:
    episode_urls = {
        entry['Name'].lower(): entry['Url']
        for entry in episode_external_urls
        if entry.get('Name') and entry.get('Url')
    }
    for series_entry in series_external_urls:
        provider = series_entry.get('Name', '').lower()
        if provider == 'imdb' and not use_imdb:
            continue
        series_url = series_entry.get('Url')
        if not series_url:
            continue
        state_url = episode_urls.get(provider)
        if not state_url and provider in ('tmdb', 'themoviedb') and season is not None:
            if episode is not None:
                state_url = f'{series_url}/season/{season}/episode/{episode}'
            else:
                state_url = f'{series_url}/season/{season}'
        return series_url, state_url
    return None, None


def resolve_movie_provider_urls(
    movie_external_urls: list[dict[str, str]], use_imdb: bool = False
) -> tuple[str | None, str | None]:
    for movie_entry in movie_external_urls:
        provider = movie_entry.get('Name', '').lower()
        if provider == 'imdb' and not use_imdb:
            continue
        if movie_url := movie_entry.get('Url'):
            return movie_url, None
    return None, None


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


async def clear_activity(
    discord_rpc: AioPresence, polling_rate: int, reason: str | None = None
) -> bool:
    try:
        await discord_rpc.clear()
        logger.info('Activity Cleared' + (f' ({reason})' if reason else ''))
        return True
    except (PyPresenceException, OSError, KeyError) as e:
        logger.error(f'Failed to Clear Activity: {type(e).__name__}')
        logger.debug(e)
        await await_connection(discord_rpc, polling_rate)
        await asyncio.sleep(polling_rate)
        return False


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

        ws_url = f'{ws_protocol}{ws_host}/socket?deviceId={device_id}'
        headers = {'Authorization': build_auth_header(device_id, jf_api_key)}
        try:
            async with session.ws_connect(ws_url, headers=headers, heartbeat=30.0) as ws:
                ws_state['ws_connected'] = True
                initial_attempt = True

                async def ping_loop() -> None:
                    while True:
                        await asyncio.sleep(30)
                        await ws.send_str(json.dumps({'MessageType': 'KeepAlive'}))

                ping_task = asyncio.create_task(ping_loop())
                try:
                    await ws.send_str(
                        json.dumps({'MessageType': 'SessionsStart', 'Data': '0,1500'})
                    )
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            if payload.get('MessageType') == 'Sessions':
                                ws_state['sessions'] = payload.get('Data', [])
                                ws_state['last_packet'] = time.time()
                                wake_event.set()
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
                finally:
                    ping_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await ping_task
        except (aiohttp.ClientError, asyncio.CancelledError, TimeoutError, ValueError) as e:
            if isinstance(e, asyncio.CancelledError):
                break
            if initial_attempt:
                logger.warning(f'Jellyfin WebSocket Error ({type(e).__name__}). Retrying...')
                logger.debug(e)
                initial_attempt = False
        finally:
            ws_state['ws_connected'] = False
        await asyncio.sleep(polling_rate)


@dataclass
class PresenceState:
    last_activity_str: str = ''
    last_playstate: bool = False
    last_playback: float | None = None  # Playback Time of Last Media Position
    last_timestamp: float | None = None  # System Time of Last Media Position
    last_rpc_update: float = 0.0  # System Time of Last RPC Activity Update
    has_pending_update: bool = False
    pending_payload: tuple[str, Any] | None = None

    def reset(self) -> None:
        self.last_activity_str = ''
        self.last_playstate = False
        self.last_playback = None
        self.last_timestamp = None
        self.last_rpc_update = time.time()
        self.has_pending_update = False


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
    show_jf_logo = config.getboolean('SHOW_JELLYFIN_LOGO', True)
    imdb_external_urls = config.getboolean('IMDB_EXTERNAL_URLS', False)

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
        await check_tmdb_auth(cache_session, tmdb_api_key)

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

    activity_str = ''
    rpc_state = PresenceState()
    last_unsupported_warning = False
    last_missing_key_warning = False

    cached_item_id = None
    cached_kwargs: dict[str, Any] = {}

    while True:
        if ws_state.get('ws_connected'):
            try:
                if rpc_state.has_pending_update:
                    remaining_cooldown = polling_rate - (time.time() - rpc_state.last_rpc_update)
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
                    ws_state['last_packet'] = time.time()
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

        if session_data:
            media_dict = session_data['NowPlayingItem']
            item_id = media_dict.get('Id')
            media_changed = item_id != cached_item_id

            try:
                session_paused = session_data['PlayState']['IsPaused']
            except KeyError as e:
                logger.warning(f'Missing Key in Session Data: {e}')
                session_paused = False
            playstate_changed = rpc_state.last_playstate != session_paused

            if session_paused and not show_when_paused:
                if rpc_state.last_activity_str:
                    if not await clear_activity(discord_rpc, polling_rate):
                        continue
                    rpc_state.reset()
                continue

            current_playback = None
            current_start = current_end = None
            try:
                position_ticks = int(session_data['PlayState']['PositionTicks'])
                current_playback = position_ticks / 10_000_000
                last_packet_time = ws_state.get('last_packet', time.time())
                adjusted_playback = current_playback + (time.time() - last_packet_time)
                current_start = int(time.time() - adjusted_playback)
                runtime_ticks = int(media_dict['RunTimeTicks'])
                if not session_paused:
                    current_end = int(current_start + runtime_ticks / 10_000_000)
            except (KeyError, TypeError, ValueError):
                pass

            STALE_GRACE_PERIOD = 5
            if current_end and time.time() >= (current_end + STALE_GRACE_PERIOD):
                if rpc_state.last_activity_str:
                    if not await clear_activity(discord_rpc, polling_rate, 'Stale Session'):
                        continue
                    rpc_state.reset()
                continue

            seek_detected, seek_delta = False, 0.0
            packet_timestamp = ws_state.get('last_packet', time.time())
            if (
                not media_changed
                and not session_paused
                and not rpc_state.last_playstate
                and current_playback is not None
                and rpc_state.last_playback is not None
                and rpc_state.last_timestamp is not None
            ):
                timestamp_elapsed = packet_timestamp - rpc_state.last_timestamp
                expected_playback = rpc_state.last_playback + timestamp_elapsed
                playback_delta = current_playback - expected_playback
                if abs(playback_delta) >= (seek_threshold - 0.5):
                    seek_detected, seek_delta = True, round(playback_delta)

            rpc_state.last_playback = current_playback
            rpc_state.last_timestamp = packet_timestamp

            if media_changed:
                cached_item_id = item_id
                try:
                    library_id = None
                    if item_id:
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
                        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
                            logger.error(
                                f'Library Retrieval Failed ({type(e).__name__}). Skipping...'
                            )
                            logger.debug(e)

                    match filter_mode:
                        case 'WHITELIST':
                            is_allowed = bool(library_id and library_id in filter_libraries)
                        case 'BLACKLIST':
                            is_allowed = not (library_id and library_id in filter_libraries)
                        case _:
                            is_allowed = True

                    if not is_allowed:
                        if rpc_state.last_activity_str:
                            if not await clear_activity(
                                discord_rpc, polling_rate, 'Library Blocked'
                            ):
                                continue
                            rpc_state.reset()
                            cached_kwargs.clear()
                        continue

                    state_str = details_str = None
                    match media_type := media_dict['Type']:
                        case 'Episode':
                            activity_type = ActivityType.WATCHING
                            season = media_dict['ParentIndexNumber']
                            episode = media_dict['IndexNumber']
                            details_str = media_dict['SeriesName']
                            state_str = f'{f"S{season}:E{episode}"} - {media_dict["Name"]}'
                            activity_str = f'{details_str} {state_str.split(" - ")[0]}'
                        case 'Movie':
                            activity_type = ActivityType.WATCHING
                            details_str = media_dict['Name']
                            if genres := media_dict.get('Genres'):
                                state_str = ' \u2022 '.join(genres[:3])
                            activity_str = details_str
                        case 'Audio':
                            activity_type = ActivityType.LISTENING
                            if artists := media_dict.get('Artists'):
                                state_str = ', '.join(artists)
                            if album_name := media_dict.get('Album'):
                                if state_str:
                                    state_str += f' - {album_name}'
                                else:
                                    state_str = album_name
                            details_str = media_dict['Name']
                            activity_str = details_str
                            if state_str:
                                activity_str += f' - {state_str.split(" - ")[0]}'
                        case _:
                            if not last_unsupported_warning:
                                logger.warning(
                                    f'Unsupported Media Type "{media_type}". Skipping...'
                                )
                                last_unsupported_warning = True
                            if rpc_state.last_activity_str:
                                if not await clear_activity(
                                    discord_rpc, polling_rate, 'Unsupported Media'
                                ):
                                    continue
                                rpc_state.reset()
                                cached_kwargs.clear()
                            continue

                    if len(details_str) < 2:
                        details_str += ' '

                    poster_url = 'large_image'
                    details_url = state_url = None
                    is_https = jf_host.startswith('https://')

                    if media_type == 'Episode':
                        tmdb_id = series_year = None
                        season = media_dict.get('ParentIndexNumber')
                        episode = media_dict.get('IndexNumber')
                        series_ids: dict[str, Any] = {}

                        series_external_urls: list[dict[str, str]] = []
                        if series_id := media_dict.get('SeriesId'):
                            try:
                                async with jf_session.get(
                                    f'{jf_host}/Items/{series_id}',
                                    headers=jf_headers,
                                    params={'userId': user_id},
                                ) as response:
                                    response.raise_for_status()
                                    series_item = await response.json()
                                    series_year = series_item.get('ProductionYear')
                                    series_ids = series_item.get('ProviderIds', {})
                                    series_external_urls = series_item.get('ExternalUrls', [])
                                    tmdb_id = series_ids.get('Tmdb') or series_ids.get('TheMovieDb')
                            except (aiohttp.ClientError, TimeoutError, ValueError):
                                pass

                        episode_external_urls: list[dict[str, str]] = []
                        if item_id:
                            try:
                                async with jf_session.get(
                                    f'{jf_host}/Items/{item_id}',
                                    headers=jf_headers,
                                    params={'userId': user_id},
                                ) as response:
                                    response.raise_for_status()
                                    episode_item = await response.json()
                                    episode_external_urls = episode_item.get('ExternalUrls', [])
                            except (aiohttp.ClientError, TimeoutError, ValueError):
                                pass

                        if not tmdb_id and tmdb_api_key:
                            logger.warning('No TMDB ID Found. Searching...')
                            if 'SeriesName' in media_dict:
                                tmdb_id = await get_series_id(
                                    cache_session,
                                    tmdb_api_key,
                                    media_dict['SeriesName'],
                                    series_year,
                                )

                        if not always_use_tmdb:
                            season_id = media_dict.get('SeasonId')
                            if season_over_series and season_id and is_https:
                                poster_url = f'{jf_host}/Items/{season_id}/Images/Primary'
                            elif series_id and is_https:
                                poster_url = f'{jf_host}/Items/{series_id}/Images/Primary'
                            elif tmdb_api_key and tmdb_id:
                                if season_over_series:
                                    poster_url = await get_season_poster(
                                        cache_session, tmdb_api_key, tmdb_id, languages, season
                                    )
                                else:
                                    poster_url = await get_series_poster(
                                        cache_session, tmdb_api_key, tmdb_id, languages
                                    )
                        elif tmdb_api_key and tmdb_id:
                            if season_over_series:
                                poster_url = await get_season_poster(
                                    cache_session, tmdb_api_key, tmdb_id, languages, season
                                )
                            else:
                                poster_url = await get_series_poster(
                                    cache_session, tmdb_api_key, tmdb_id, languages
                                )

                        details_url, state_url = resolve_series_provider_urls(
                            series_external_urls,
                            episode_external_urls,
                            season,
                            episode,
                            use_imdb=imdb_external_urls,
                        )
                        if not details_url and tmdb_id:
                            details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                            if season is not None:
                                if episode is not None:
                                    state_url = f'{details_url}/season/{season}/episode/{episode}'
                                else:
                                    state_url = f'{details_url}/season/{season}'

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

                        if not always_use_tmdb and item_id and is_https:
                            poster_url = f'{jf_host}/Items/{item_id}/Images/Primary'
                        elif tmdb_api_key and tmdb_id:
                            poster_url = await get_movie_poster(
                                cache_session, tmdb_api_key, tmdb_id, languages
                            )

                        movie_external_urls = media_dict.get('ExternalUrls', [])
                        details_url, state_url = resolve_movie_provider_urls(
                            movie_external_urls,
                            use_imdb=imdb_external_urls,
                        )
                        if not details_url and tmdb_id:
                            details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'

                    elif media_type == 'Audio':
                        music_ids = media_dict.get('ProviderIds', {})
                        track_id = music_ids.get('MusicBrainzTrack')
                        group_id = music_ids.get('MusicBrainzReleaseGroup')
                        release_id = music_ids.get('MusicBrainzAlbum')

                        album_id = media_dict.get('AlbumId')
                        if album_id and (not group_id or release_over_group and not release_id):
                            try:
                                async with jf_session.get(
                                    f'{jf_host}/Items/{album_id}',
                                    headers=jf_headers,
                                    params={'userId': user_id},
                                ) as response:
                                    response.raise_for_status()
                                    album_item = await response.json()
                                    album_music_ids = album_item.get('ProviderIds', {})
                                    if not group_id:
                                        group_id = album_music_ids.get('MusicBrainzReleaseGroup')
                                    if release_over_group and not release_id:
                                        release_id = album_music_ids.get('MusicBrainzAlbum')
                            except (aiohttp.ClientError, TimeoutError, ValueError):
                                pass

                        if not group_id and release_id:
                            group_id = await get_music_id_from_release(cache_session, release_id)
                        if not group_id:
                            logger.warning('No MusicBrainz ID Found. Searching...')
                            if 'AlbumArtist' in media_dict and 'Album' in media_dict:
                                group_id = await get_music_id_from_search(
                                    cache_session, media_dict['AlbumArtist'], media_dict['Album']
                                )

                        if not always_use_musicbrainz and album_id and is_https:
                            poster_url = f'{jf_host}/Items/{album_id}/Images/Primary'
                        elif group_id:
                            cover_release_id = release_id if release_over_group else None
                            poster_url = await get_release_cover(
                                cache_session, group_id, cover_release_id
                            )

                        if track_id:
                            details_url = f'https://musicbrainz.org/track/{track_id}'
                        if release_over_group and release_id:
                            state_url = f'https://musicbrainz.org/release/{release_id}'
                        elif group_id:
                            state_url = f'https://musicbrainz.org/release-group/{group_id}'

                    display_name = server_name
                    if (
                        show_server_name
                        and server_name is not None
                        and activity_type == ActivityType.WATCHING
                    ):
                        display_name = f'on {server_name}'

                    cached_kwargs = {
                        'activity_type': activity_type,
                        'status_display_type': StatusDisplayType.DETAILS,
                        'name': display_name,
                        'details': details_str[:128] if details_str else None,
                        'details_url': details_url,
                        'state': state_str[:128] if state_str else None,
                        'state_url': state_url,
                        'large_image': poster_url,
                    }

                except KeyError as e:
                    if not last_missing_key_warning:
                        logger.warning(f'Missing Key in Session Data: {e}. Skipping...')
                        last_missing_key_warning = True
                    cached_item_id = None
                    await asyncio.sleep(polling_rate)
                    continue

                last_unsupported_warning = False
                last_missing_key_warning = False

            if media_changed or seek_detected or playstate_changed:
                rpc_state.has_pending_update = True
                if media_changed:
                    rpc_state.pending_payload = ('media_changed', activity_str)
                elif seek_detected:
                    delta_str = f'+{seek_delta}s' if seek_delta > 0 else f'{seek_delta}s'
                    rpc_state.pending_payload = ('seek_detected', delta_str)
                elif playstate_changed:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    rpc_state.pending_payload = ('playstate_changed', playstate)

            if (
                rpc_state.has_pending_update
                and (time.time() - rpc_state.last_rpc_update) >= polling_rate
            ):
                small_image = (
                    'media_paused' if session_paused else 'small_image' if show_jf_logo else None
                )

                if rpc_state.pending_payload:
                    update_type, payload = rpc_state.pending_payload
                    match update_type:
                        case 'media_changed':
                            logger.info(f'"{payload}"')
                        case 'seek_detected':
                            logger.log(15, f'Seek Detected ({payload})')
                        case 'playstate_changed':
                            logger.log(15, f'PlayState {payload}')
                    rpc_state.pending_payload = None

                try:
                    await discord_rpc.update(
                        **cached_kwargs,
                        start=current_start,
                        end=current_end,
                        small_image=small_image,
                    )
                    rpc_state.last_rpc_update = time.time()
                    rpc_state.last_activity_str = activity_str
                    rpc_state.last_playstate = session_paused
                    rpc_state.has_pending_update = False
                except (PyPresenceException, OSError, KeyError) as e:
                    logger.error(f'RPC Update Error: {type(e).__name__}')
                    logger.debug(e)
                    await await_connection(discord_rpc, polling_rate)
                    await asyncio.sleep(polling_rate)
                    continue

        elif rpc_state.last_activity_str:
            if not await clear_activity(discord_rpc, polling_rate):
                continue
            rpc_state.reset()
            cached_item_id = None
            cached_kwargs.clear()

        if not ws_state.get('ws_connected'):
            await asyncio.sleep(polling_rate)


async def monitor_activity(
    config: SectionProxy, ini_path: str, polling_rate: int, seek_threshold: int
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
            jf_host = config['JELLYFIN_HOST'].rstrip('/')
            if urlparse(jf_host).scheme not in ('http', 'https'):
                jf_host = await resolve_jf_host(jf_host, jf_session)
                config['JELLYFIN_HOST'] = jf_host
                logger.warning(f'Missing URL Protocol: {jf_host}')

            ws_task = asyncio.create_task(
                ws_listener(jf_session, config, polling_rate, ws_state, wake_event)
            )

            try:
                await activity_loop(
                    jf_session,
                    cache_session,
                    discord_rpc,
                    config,
                    ini_path,
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
    polling_rate = max(1, config.getint('POLLING_RATE', 5))
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
