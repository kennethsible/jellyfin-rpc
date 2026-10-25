import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import queue
import re
import ssl
import sys
import threading
import time
import uuid
from configparser import ConfigParser, SectionProxy
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from importlib.metadata import metadata
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from typing import Any, cast
from urllib.parse import urlparse

import aiohttp
import certifi
import pycountry
from aiohttp import ClientSession, WSMsgType
from aiohttp_client_cache.backends.base import CacheBackend
from aiohttp_client_cache.session import CachedSession
from pypresence.exceptions import PyPresenceException
from pypresence.presence import AioPresence
from pypresence.types import ActivityType, StatusDisplayType

_pycountry: Any = pycountry

CLIENT_ID = '1238889120672120853'
logger = logging.getLogger('RPC')
logging.addLevelName(15, 'VERBOSE')

pkg_metadata = metadata('jellyfin-rpc')
contact_info = parseaddr(pkg_metadata['Author-email'])[1]
RPC_VERSION = pkg_metadata['Version']
USER_AGENT = f'Jellyfin-RPC/{RPC_VERSION} ( {contact_info} )'


def load_config(ini_path: str) -> SectionProxy:
    config_parser = ConfigParser()
    config_parser.read(ini_path, encoding='utf-8')
    has_legacy_keys = False

    def migrate_key(legacy_key: str, modern_key: str) -> None:
        nonlocal has_legacy_keys
        if config_parser.has_option('DEFAULT', legacy_key):
            if not config_parser.has_option('DEFAULT', modern_key):
                value = config_parser.get('DEFAULT', legacy_key)
                config_parser.set('DEFAULT', modern_key, value)
            config_parser.remove_option('DEFAULT', legacy_key)
            has_legacy_keys = True

    migrate_key('API_TOKEN', 'JELLYFIN_API_KEY')
    migrate_key('USERNAME', 'JELLYFIN_USERNAME')
    migrate_key('FILTER_MODE', 'LIBRARY_FILTER_TYPE')
    migrate_key('FILTER_LIBRARIES', 'SELECTED_LIBRARIES')
    migrate_key('ALWAYS_USE_MUSICBRAINZ', 'ALWAYS_USE_COVERART')
    migrate_key('IMDB_EXTERNAL_URLS', 'IMDB_EXTERNAL_LINKS')
    migrate_key('REFRESH_RATE', 'POLLING_RATE')
    migrate_key('FILE_HDLR_LEVEL', 'LOG_LEVEL_FILE')

    if has_legacy_keys:
        with open(ini_path, 'w', encoding='utf-8') as ini_file:
            config_parser.write(ini_file)

    return config_parser['DEFAULT']


def save_config(config_parser: ConfigParser, ini_path: str) -> None:
    with open(ini_path, 'w', encoding='utf-8') as ini_file:
        config_parser.write(ini_file)


def parse_delimited_list(config: SectionProxy, option: str) -> list[str]:
    option_split = re.split(r'[,;|]', config.get(option, ''))
    return [x.strip() for x in option_split if x.strip()]


def get_valid_level(level_str: str, default: int) -> int:
    level_mapping = logging.getLevelNamesMapping()
    return level_mapping.get(level_str.upper().strip(), default)


def sanitize_discord_str(presence_var: str | None) -> str | None:
    if presence_var is None:
        return None
    presence_var = presence_var.strip()
    if len(presence_var) == 1:
        return f'{presence_var} '
    return presence_var[:128]


def sanitize_broadcast_title(title_str: str | None) -> str | None:
    if title_str is None:
        return None
    clean_str = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title_str)
    pattern = r'\b(1080p|1080i|720p|4k|uhd|fhd|hd|sd|fps|60fps|30fps|hevc|h264|x264)\b'
    clean_str = re.sub(pattern, '', clean_str, flags=re.IGNORECASE)
    clean_str = re.sub(r'\s+', ' ', clean_str).strip(' -:|')
    return clean_str or title_str


def get_lang_code(lang_str: str) -> str | None:
    if not (lang_str := lang_str.strip()):
        return None
    if len(lang_str) == 2 and lang_str.isalpha():
        lang = _pycountry.languages.get(alpha_2=lang_str.lower())
        if lang and hasattr(lang, 'alpha_2'):
            return str(lang.alpha_2)
    try:
        match = _pycountry.languages.lookup(lang_str)
        if hasattr(match, 'alpha_2'):
            return str(match.alpha_2)
    except LookupError:
        pass
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
            logger.info(f'{jf_host.rstrip("/")}/web/#/quickconnect')
    except (aiohttp.ClientError, TimeoutError, JSONDecodeError, KeyError) as e:
        logger.error(f'Failed to Initiate Quick Connect ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
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
        except (aiohttp.ClientError, TimeoutError, JSONDecodeError, KeyError):
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
    except (aiohttp.ClientError, TimeoutError, JSONDecodeError, KeyError) as e:
        logger.error(f'Failed to Retrieve User Access Token ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
        sys.exit(1)


async def resolve_host_url(session: ClientSession, jf_host: str) -> str:
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


async def await_jellyfin_connect(
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
        logger.debug(type(e).__name__, exc_info=True)
        sys.exit(1)

    device_id = get_device_id(config)
    if not jf_api_key:
        jf_api_key, jf_username = await initiate_quick_connect(session, jf_host, device_id)

        config['JELLYFIN_API_KEY'] = jf_api_key
        config['JELLYFIN_USERNAME'] = jf_username

        config_parser = ConfigParser()
        config_parser.read(ini_path, encoding='utf-8')
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
                if jf_username == user.get('Name'):
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

        except (aiohttp.ClientError, TimeoutError) as e:
            if initial_attempt:
                logger.error(f'Jellyfin API Network Error ({type(e).__name__}). Retrying...')
                logger.debug(type(e).__name__, exc_info=True)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue
        except (ValueError, KeyError) as e:
            if initial_attempt:
                logger.error(f'Jellyfin API Parsing Error ({type(e).__name__}). Retrying...')
                logger.debug(type(e).__name__, exc_info=True)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue


async def check_tmdb_auth(session: ClientSession, api_key: str) -> None:
    config_url = 'https://api.themoviedb.org/3/configuration'
    config_params = {'api_key': api_key}
    try:
        async with session.get(config_url, params=config_params) as response:
            if response.status == 401:
                logger.warning('TMDB API Key Rejected (HTTP 401)')
                return
            if response.status == 429:
                logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                return
            response.raise_for_status()
        logger.info('Connected to TMDB API')
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)


async def get_imdb_episode_id(
    session: ClientSession, api_key: str, tmdb_id: str, season: int, episode: int
) -> str | None:
    search_url = (
        f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/episode/{episode}/external_ids'
    )
    try:
        async with session.get(search_url, params={'api_key': api_key}) as response:
            if response.status == 404:
                logger.log(15, 'No Episode ID Found on IMDb')
                return None
            if response.status == 429:
                logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                return None
            response.raise_for_status()
            data = await response.json()
            return cast(str | None, data.get('imdb_id'))
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return None


async def get_series_id(
    session: ClientSession, api_key: str, title: str, year: int | None = None
) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/tv'
    search_params = {'api_key': api_key, 'query': title}
    if year is not None:
        search_params['first_air_date_year'] = str(year)
    try:
        async with session.get(search_url, params=search_params) as response:
            if response.status == 404:
                logger.log(15, 'No Series ID Found on TMDB')
                return None
            if response.status == 429:
                logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                return None
            response.raise_for_status()
            data = await response.json()
            if results := data.get('results'):
                return cast(str | None, results[0].get('id'))
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
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
            if response.status == 404:
                logger.log(15, 'No Movie ID Found on TMDB')
                return None
            if response.status == 429:
                logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                return None
            response.raise_for_status()
            data = await response.json()
            if results := data.get('results'):
                return cast(str | None, results[0].get('id'))
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
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
            if response.status == 404:
                logger.log(15, 'No Release Group ID Found on MusicBrainz')
                return None
            if response.status == 429:
                logger.warning('MusicBrainz Rate Limit Exceeded (HTTP 429)')
                return None
            response.raise_for_status()
            data = await response.json()
            if release_groups := data.get('release-groups'):
                return cast(str | None, release_groups[0].get('id'))
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'MusicBrainz API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'MusicBrainz API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'MusicBrainz API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return None


async def get_music_id_from_release(session: ClientSession, release_id: str) -> str | None:
    search_url = f'https://musicbrainz.org/ws/2/release/{release_id}'
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    params = {'inc': 'release-groups', 'fmt': 'json'}
    try:
        async with session.get(search_url, headers=headers, params=params) as response:
            if response.status == 404:
                logger.log(15, 'No Release Group ID Found on MusicBrainz')
                return None
            if response.status == 429:
                logger.warning('MusicBrainz Rate Limit Exceeded (HTTP 429)')
                return None
            response.raise_for_status()
            data = await response.json()
            if release_group := data.get('release-group'):
                return cast(str | None, release_group.get('id'))
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'MusicBrainz API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'MusicBrainz API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'MusicBrainz API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
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

    posters_by_lang: dict[str | None, list[dict[str, Any]]] = {}
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
                if response.status == 404:
                    logger.log(15, f'Series {tmdb_id} Not Found on TMDB')
                    return 'large_image'
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return f'https://image.tmdb.org/t/p/w185/{poster["file_path"]}'
                logger.warning('No Poster Available on TMDB')
        else:
            series_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}'
            async with session.get(series_url, params={'api_key': api_key}) as response:
                if response.status == 404:
                    logger.log(15, f'Series {tmdb_id} Not Found on TMDB')
                    return 'large_image'
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return f'https://image.tmdb.org/t/p/w185/{poster_path}'
                logger.warning('No Poster Available on TMDB')
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
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
                if response.status == 404:
                    logger.log(15, f'Season {season} Not Found on TMDB. Trying Series...')
                    return await get_series_poster(session, api_key, tmdb_id, languages)
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return f'https://image.tmdb.org/t/p/w185/{poster["file_path"]}'
                logger.warning('No Poster Available on TMDB')
        else:
            season_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}'
            async with session.get(season_url, params={'api_key': api_key}) as response:
                if response.status == 404:
                    logger.log(15, f'Season {season} Not Found on TMDB')
                    return await get_series_poster(session, api_key, tmdb_id, languages)
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return f'https://image.tmdb.org/t/p/w185/{poster_path}'
                logger.warning('No Poster Available on TMDB')
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
        return 'large_image'
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
        return 'large_image'
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return await get_series_poster(session, api_key, tmdb_id, languages)


async def get_movie_poster(
    session: ClientSession, api_key: str, tmdb_id: str, languages: list[str]
) -> str:
    try:
        if languages:
            images_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}/images'
            async with session.get(images_url, params={'api_key': api_key}) as response:
                if response.status == 404:
                    logger.log(15, f'Movie {tmdb_id} Not Found on TMDB')
                    return 'large_image'
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster := select_poster(data['posters'], languages):
                    return f'https://image.tmdb.org/t/p/w185/{poster["file_path"]}'
                logger.warning('No Poster Available on TMDB')
        else:
            movie_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}'
            async with session.get(movie_url, params={'api_key': api_key}) as response:
                if response.status == 404:
                    logger.log(15, f'Movie {tmdb_id} Not Found on TMDB')
                    return 'large_image'
                if response.status == 429:
                    logger.warning('TMDB API Rate Limit Exceeded (HTTP 429)')
                    return 'large_image'
                response.raise_for_status()
                data = await response.json()
                if poster_path := data.get('poster_path'):
                    return f'https://image.tmdb.org/t/p/w185/{poster_path}'
                logger.warning('No Poster Available on TMDB')
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'TMDB API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'TMDB API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'TMDB API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return 'large_image'


async def get_release_group_cover(session: ClientSession, group_id: str) -> str:
    try:
        async with session.get(f'https://coverartarchive.org/release-group/{group_id}') as response:
            if response.status == 404:
                logger.log(15, f'Release Group {group_id} Not Found on Cover Art Archive')
                return 'large_image'
            if response.status == 429:
                logger.warning('Cover Art Archive API Rate Limit Exceeded (HTTP 429)')
                return 'large_image'
            response.raise_for_status()
            data = await response.json()
            images = data.get('images', [])
            if images and 'image' in images[0]:
                return str(images[0]['image'])
            logger.warning('No Cover Art Available on Cover Art Archive')
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'Cover Art Archive API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'Cover Art Archive API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'Cover Art Archive API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return 'large_image'


async def get_release_cover(
    session: ClientSession, group_id: str, release_id: str | None = None
) -> str:
    if not release_id:
        return await get_release_group_cover(session, group_id)
    try:
        async with session.get(f'https://coverartarchive.org/release/{release_id}') as response:
            if response.status == 404:
                logger.log(
                    15,
                    f'Release {release_id} Not Found on Cover Art Archive. Trying Release Group...',
                )
                return await get_release_group_cover(session, group_id)
            if response.status == 429:
                logger.warning('Cover Art Archive API Rate Limit Exceeded (HTTP 429)')
                return 'large_image'
            response.raise_for_status()
            data = await response.json()
            images = data.get('images', [])
            if images and 'image' in images[0]:
                return str(images[0]['image'])
    except aiohttp.ClientResponseError as e:
        obfuscated_url = e.request_info.real_url.with_query(None)
        logger.error(f'Cover Art Archive API HTTP Error ({e.status}: {e.message})')
        logger.debug(f'HTTP {e.status} at {obfuscated_url}')
        return 'large_image'
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.error(f'Cover Art Archive API Network Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
        return 'large_image'
    except (ValueError, KeyError, IndexError) as e:
        logger.error(f'Cover Art Archive API Parsing Error ({type(e).__name__})')
        logger.debug(type(e).__name__, exc_info=True)
    return await get_release_group_cover(session, group_id)


def resolve_series_provider_urls(
    series_external_urls: list[dict[str, str]],
    episode_external_urls: list[dict[str, str]],
    season: int | None = None,
    episode: int | None = None,
    use_imdb: bool = False,
    episode_imdb_id: str | None = None,
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
        if not state_url and season is not None:
            if provider == 'imdb':
                if episode_imdb_id:
                    state_url = f'https://www.imdb.com/title/{episode_imdb_id}'
                elif season > 0:
                    state_url = f'{series_url.rstrip("/")}/episodes?season={season}'
                else:
                    state_url = f'{series_url.rstrip("/")}/episodes'
            elif provider in ('tmdb', 'themoviedb'):
                if episode is not None:
                    state_url = f'{series_url.rstrip("/")}/season/{season}/episode/{episode}'
                else:
                    state_url = f'{series_url.rstrip("/")}/season/{season}'
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


def check_discord_connect(discord_rpc: AioPresence) -> bool:
    reader = getattr(discord_rpc, 'sock_reader', None)
    writer = getattr(discord_rpc, 'sock_writer', None)
    if reader is None or writer is None:
        return False
    return not (writer.is_closing() or reader.at_eof() or reader.exception() is not None)


async def await_discord_connect(discord_rpc: AioPresence, polling_rate: int) -> None:
    initial_attempt = True
    while True:
        try:
            await discord_rpc.connect()
            logger.info('Connected to Discord Client')
        except (PyPresenceException, OSError) as e:
            if initial_attempt:
                logger.error(f'Discord Client Connection Error ({type(e).__name__}). Retrying...')
                logger.debug(type(e).__name__, exc_info=True)
            initial_attempt = False
            await asyncio.sleep(polling_rate)
            continue
        break


async def clear_activity(
    discord_rpc: AioPresence, polling_rate: int, stale_session: bool = False
) -> bool:
    try:
        await discord_rpc.clear()
        if stale_session:
            logger.info('Activity Cleared (Stale Session)')
        else:
            logger.info('Activity Cleared')
        return True
    except (PyPresenceException, OSError, KeyError) as e:
        logger.error(f'RPC Clear Error: {type(e).__name__}: {e}')
        logger.debug(type(e).__name__, exc_info=True)
        await await_discord_connect(discord_rpc, polling_rate)
        await asyncio.sleep(polling_rate)
        return False


async def websocket_listener(
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
        if not (jf_api_key := config.get('JELLYFIN_API_KEY')):
            await asyncio.sleep(1)
            continue

        ws_url = f'{ws_protocol}{ws_host}/socket?deviceId={device_id}'
        headers = {'Authorization': build_auth_header(device_id, jf_api_key)}
        try:
            async with session.ws_connect(ws_url, headers=headers, heartbeat=30.0) as websocket:
                ws_state['ws_connected'] = True
                initial_attempt = True

                async def ping_loop() -> None:
                    while True:
                        await asyncio.sleep(30)
                        await websocket.send_str(json.dumps({'MessageType': 'KeepAlive'}))

                ping_task = asyncio.create_task(ping_loop())
                try:
                    await websocket.send_str(
                        json.dumps({'MessageType': 'SessionsStart', 'Data': '0,1500'})
                    )
                    async for message in websocket:
                        if message.type == WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            if payload.get('MessageType') == 'Sessions':
                                ws_state['sessions'] = payload.get('Data', [])
                                ws_state['last_packet'] = time.time()
                                wake_event.set()
                        elif message.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
                finally:
                    ping_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await ping_task
        except asyncio.CancelledError:
            break
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            if initial_attempt:
                logger.debug(type(e).__name__, exc_info=True)
                initial_attempt = False
        finally:
            ws_state['ws_connected'] = False
        await asyncio.sleep(polling_rate)


@dataclass
class PresenceState:
    last_activity_str: str = ''
    last_paused_state: bool = False
    last_playback_position: float | None = None
    last_packet_time: float | None = None
    last_update_time: float = 0.0
    has_pending_update: bool = False
    pending_payload: tuple[str, Any] | None = None

    def reset(self, paused_state: bool = False) -> None:
        self.last_activity_str = ''
        self.last_paused_state = paused_state
        self.last_playback_position = None
        self.last_packet_time = None
        self.last_update_time = time.time()
        self.has_pending_update = False


async def activity_loop(
    jf_session: ClientSession,
    cache_session: ClientSession,
    discord_rpc: AioPresence,
    config: SectionProxy,
    ini_path: str,
    polling_rate: int,
    seek_threshold: int,
    session_timeout: int,
    stale_grace_period: int,
    ws_state: dict[str, Any],
    wake_event: asyncio.Event,
) -> None:
    jf_host = config['JELLYFIN_HOST'].rstrip('/')
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_server_name = config.getboolean('SHOW_SERVER_NAME', False)
    show_jf_logo = config.getboolean('SHOW_JELLYFIN_LOGO', True)
    imdb_external_urls = config.getboolean('IMDB_EXTERNAL_LINKS', False)

    user_id, server_name = await await_jellyfin_connect(
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

    musicbrainz_search = config.getboolean('MUSICBRAINZ_SEARCH', True)
    always_use_coverart = config.getboolean('ALWAYS_USE_COVERART', False)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', False)

    library_filter_type = config.get('LIBRARY_FILTER_TYPE', 'DENYLIST').upper()
    selected_libraries = parse_delimited_list(config, 'SELECTED_LIBRARIES')

    media_types = parse_delimited_list(config, 'MEDIA_TYPES')
    type_mapping = {'Movies': 'Movie', 'Shows': 'Episode', 'Music': 'Audio', 'LiveTV': 'TvChannel'}

    jf_media_types: dict[str, int] = {}
    for i, media_type in enumerate(media_types):
        if media_type in type_mapping:
            priority_level = len(media_types) - i
            jf_media_type = type_mapping[media_type]
            jf_media_types[jf_media_type] = priority_level

    activity_str = ''
    rpc_state = PresenceState()
    current_session_id: str | None = None
    missing_session_keys: set[str] = set()

    cached_media_id = None
    cached_kwargs: dict[str, Any] = {}
    cached_program_dict: dict[str, Any] = {}

    while True:
        if ws_state.get('ws_connected'):
            try:
                current_time = time.time()
                elapsed_since_update = current_time - rpc_state.last_update_time
                if rpc_state.has_pending_update or (
                    rpc_state.last_activity_str and elapsed_since_update < polling_rate
                ):
                    wait_timeout = max(0.05, polling_rate - elapsed_since_update)
                elif (
                    current_session_id is None
                    and rpc_state.last_activity_str
                    and rpc_state.last_packet_time is not None
                ):
                    remaining_timeout = (
                        rpc_state.last_packet_time + session_timeout
                    ) - current_time
                    wait_timeout = max(0.05, min(polling_rate, remaining_timeout))
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
                logger.debug(type(e).__name__, exc_info=True)
                user_id, server_name = await await_jellyfin_connect(
                    jf_session, config, ini_path, show_server_name, polling_rate
                )
                jf_username = config['JELLYFIN_USERNAME']
                jf_api_key = config['JELLYFIN_API_KEY']
                jf_headers['Authorization'] = build_auth_header(get_device_id(config), jf_api_key)
                await asyncio.sleep(polling_rate)
                continue
            except ValueError as e:
                logger.error(f'Session Parsing Error: {type(e).__name__}')
                logger.debug(type(e).__name__, exc_info=True)
                await asyncio.sleep(polling_rate)
                continue

        filtered_sessions: list[dict[str, Any]] = []
        for session in sessions:
            if session.get('UserName') != jf_username:
                continue
            media_dict = session.get('NowPlayingItem', {})
            media_type = media_dict.get('Type')
            if media_type not in jf_media_types:
                continue
            filtered_sessions.append(session)

        session_dict: dict[str, Any] = {}
        if filtered_sessions:

            def session_sort_key(
                session: dict[str, Any], session_id: str | None = current_session_id
            ) -> tuple[Any, ...]:
                playstate = session.get('PlayState', {})
                media_dict = session.get('NowPlayingItem', {})
                media_type = media_dict.get('Type', '')

                is_playing = not playstate.get('IsPaused', False)
                priority_level = jf_media_types.get(media_type, 0)
                is_current = 1 if session.get('Id') == session_id else 0
                activity_date = session.get('LastActivityDate', '')

                return (is_playing, priority_level, is_current, activity_date)

            filtered_sessions.sort(key=session_sort_key, reverse=True)
            session_dict = filtered_sessions[0]
            current_session_id = session_dict.get('Id')
        else:
            current_session_id = None

        if not check_discord_connect(discord_rpc):
            await await_discord_connect(discord_rpc, polling_rate)
            rpc_state.last_activity_str = ''
            rpc_state.last_update_time = 0.0
            if cached_kwargs:
                rpc_state.has_pending_update = True

        if session_dict:
            media_dict = session_dict.get('NowPlayingItem', {})
            media_type = media_dict.get('Type')
            media_id = media_dict.get('Id')
            media_changed = media_id != cached_media_id

            if (
                not media_changed
                and media_type == 'TvChannel'
                and cached_program_dict.get('EndDate')
            ):
                try:
                    program_end = datetime.fromisoformat(cached_program_dict['EndDate']).timestamp()
                    if time.time() >= program_end:
                        media_changed = True
                except (ValueError, TypeError):
                    pass

            try:
                session_paused = session_dict['PlayState']['IsPaused']
            except KeyError as e:
                logger.debug(type(e).__name__, exc_info=True)

            playstate_changed = rpc_state.last_paused_state != session_paused

            if session_paused and not show_when_paused:
                if rpc_state.last_activity_str:
                    if (time.time() - rpc_state.last_update_time) < polling_rate:
                        continue
                    if not await clear_activity(discord_rpc, polling_rate):
                        continue
                    logger.log(15, 'PlayState Paused')
                    rpc_state.reset(paused_state=True)
                continue

            if media_type == 'TvChannel':
                if media_changed:
                    cached_program_dict.clear()
                    if media_id:
                        try:
                            async with jf_session.get(
                                f'{jf_host}/Items/{media_id}',
                                headers=jf_headers,
                                params={'userId': user_id},
                            ) as response:
                                response.raise_for_status()
                                channel = await response.json()
                                cached_program_dict = channel.get('CurrentProgram', {})
                        except (aiohttp.ClientError, TimeoutError) as e:
                            logger.error(f'Program Retrieval Network Error ({type(e).__name__})')
                            logger.debug(type(e).__name__, exc_info=True)
                        except (ValueError, KeyError) as e:
                            logger.error(f'Program Retrieval Parsing Error ({type(e).__name__})')
                            logger.debug(type(e).__name__, exc_info=True)
            else:
                cached_program_dict.clear()

            current_playback = None
            current_start = current_end = None
            try:
                if media_type == 'TvChannel':
                    if start_date := cached_program_dict.get('StartDate'):
                        current_start = int(datetime.fromisoformat(start_date).timestamp())
                    if (end_date := cached_program_dict.get('EndDate')) and not session_paused:
                        current_end = int(datetime.fromisoformat(end_date).timestamp())
                else:
                    position_ticks = int(session_dict['PlayState']['PositionTicks'])
                    current_playback = position_ticks / 10_000_000
                    last_packet_time = ws_state.get('last_packet', time.time())
                    adjusted_playback = current_playback + (time.time() - last_packet_time)
                    current_start = int(time.time() - adjusted_playback)
                    runtime_ticks = int(media_dict['RunTimeTicks'])
                    if not session_paused:
                        current_end = int(current_start + runtime_ticks / 10_000_000)
            except (ValueError, KeyError, TypeError) as e:
                logger.debug(type(e).__name__, exc_info=True)

            if current_end and time.time() >= (current_end + stale_grace_period):
                if rpc_state.last_activity_str:
                    if (time.time() - rpc_state.last_update_time) < polling_rate:
                        continue
                    if not await clear_activity(discord_rpc, polling_rate, stale_session=True):
                        continue
                    rpc_state.reset()
                    cached_media_id = None
                    cached_kwargs.clear()
                continue

            seek_detected, seek_delta = False, 0.0
            packet_timestamp = ws_state.get('last_packet', time.time())
            if (
                not media_changed
                and not session_paused
                and not rpc_state.last_paused_state
                and current_playback is not None
                and rpc_state.last_playback_position is not None
                and rpc_state.last_packet_time is not None
            ):
                timestamp_elapsed = packet_timestamp - rpc_state.last_packet_time
                expected_playback = rpc_state.last_playback_position + timestamp_elapsed
                playback_delta = current_playback - expected_playback
                position_delta = current_playback - rpc_state.last_playback_position
                if abs(playback_delta) >= (seek_threshold - 0.5) or position_delta <= -0.5:
                    seek_detected, seek_delta = True, round(playback_delta)

            position_acquired = (
                rpc_state.last_playback_position is None
                and current_playback is not None
                and not session_paused
            )
            rpc_state.last_playback_position = current_playback
            rpc_state.last_packet_time = packet_timestamp

            if media_changed:
                try:
                    library_id = None
                    if selected_libraries and media_id:
                        try:
                            ancestors_url = f'{jf_host}/Items/{media_id}/Ancestors'
                            async with jf_session.get(
                                ancestors_url, headers=jf_headers, params={'userId': user_id}
                            ) as response:
                                response.raise_for_status()
                                ancestors = await response.json()
                            for ancestor in ancestors:
                                if ancestor.get('Type') in ('CollectionFolder', 'AggregateFolder'):
                                    library_id = ancestor.get('Id')
                                    break
                        except (aiohttp.ClientError, TimeoutError) as e:
                            logger.error(f'Library Retrieval Network Error ({type(e).__name__})')
                            logger.debug(type(e).__name__, exc_info=True)
                        except (ValueError, KeyError) as e:
                            logger.error(f'Library Retrieval Parsing Error ({type(e).__name__})')
                            logger.debug(type(e).__name__, exc_info=True)

                        match library_filter_type:
                            case 'ALLOWLIST' | 'WHITELIST':
                                is_allowed = bool(library_id and library_id in selected_libraries)
                            case 'DENYLIST' | 'BLACKLIST':
                                is_allowed = not (library_id and library_id in selected_libraries)
                            case _:
                                is_allowed = True

                        if not is_allowed:
                            cached_kwargs.clear()
                            if rpc_state.last_activity_str:
                                if (time.time() - rpc_state.last_update_time) < polling_rate:
                                    continue
                                if not await clear_activity(discord_rpc, polling_rate):
                                    continue
                                rpc_state.reset()
                            cached_media_id = media_id
                            continue

                    state_str = details_str = None
                    match media_type:
                        case 'Episode':
                            activity_type = ActivityType.WATCHING
                            season = media_dict['ParentIndexNumber']
                            episode = media_dict['IndexNumber']
                            details_str = media_dict['SeriesName']
                            state_str = f'{f"S{season}:E{episode}"} \u2022 {media_dict["Name"]}'
                            activity_str = f'{details_str} {state_str.split(" \u2022 ")[0]}'
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
                                    state_str += f' \u2022 {album_name}'
                                else:
                                    state_str = album_name
                            details_str = media_dict['Name']
                            activity_str = details_str
                            if state_str:
                                activity_str += f' \u2022 {state_str.split(" \u2022 ")[0]}'
                        case 'TvChannel':
                            activity_type = ActivityType.WATCHING
                            channel_name = (
                                sanitize_broadcast_title(
                                    media_dict.get('ChannelName') or media_dict.get('Name')
                                )
                                or 'Live TV'
                            )
                            program_dict = cached_program_dict
                            program_name = sanitize_broadcast_title(program_dict.get('Name'))
                            season = program_dict.get('ParentIndexNumber')
                            episode = program_dict.get('IndexNumber')
                            details_str = program_name or channel_name
                            if season is not None and episode is not None:
                                state_str = f'S{season}:E{episode} ({channel_name})'
                                activity_str = f'{details_str} S{season}:E{episode}'
                            elif program_name:
                                state_str = f'{channel_name}'
                                activity_str = f'{details_str} ({channel_name})'
                            else:
                                state_str = 'Live Broadcast'
                                activity_str = details_str

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
                                    series_dict = await response.json()
                                    series_year = series_dict.get('ProductionYear')
                                    series_ids = series_dict.get('ProviderIds', {})
                                    series_external_urls = series_dict.get('ExternalUrls', [])
                                    tmdb_id = series_ids.get('Tmdb') or series_ids.get('TheMovieDb')
                            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
                                logger.debug(
                                    f'Failed to Fetch Series Details ({series_id})', exc_info=True
                                )

                        episode_external_urls: list[dict[str, str]] = []
                        if media_id:
                            try:
                                async with jf_session.get(
                                    f'{jf_host}/Items/{media_id}',
                                    headers=jf_headers,
                                    params={'userId': user_id},
                                ) as response:
                                    response.raise_for_status()
                                    episode_dict = await response.json()
                                    episode_external_urls = episode_dict.get('ExternalUrls', [])
                            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
                                logger.debug(
                                    f'Failed to Fetch Episode Details ({media_id})', exc_info=True
                                )

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
                                poster_url = f'{jf_host}/Items/{season_id}/Images/Primary/0'
                            elif series_id and is_https:
                                poster_url = f'{jf_host}/Items/{series_id}/Images/Primary/0'
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

                        episode_imdb_id = None
                        if (
                            imdb_external_urls
                            and tmdb_id
                            and tmdb_api_key
                            and season is not None
                            and episode is not None
                            and not any(
                                url.get('Name', '').lower() == 'imdb'
                                for url in episode_external_urls
                            )
                        ):
                            episode_imdb_id = await get_imdb_episode_id(
                                cache_session, tmdb_api_key, tmdb_id, season, episode
                            )

                        details_url, state_url = resolve_series_provider_urls(
                            series_external_urls,
                            episode_external_urls,
                            season,
                            episode,
                            use_imdb=imdb_external_urls,
                            episode_imdb_id=episode_imdb_id,
                        )
                        if not details_url and tmdb_id:
                            details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                            if not state_url and season is not None:
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

                        if not always_use_tmdb and media_id and is_https:
                            poster_url = f'{jf_host}/Items/{media_id}/Images/Primary/0'
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
                                    album_dict = await response.json()
                                    album_music_ids = album_dict.get('ProviderIds', {})
                                    if not group_id:
                                        group_id = album_music_ids.get('MusicBrainzReleaseGroup')
                                    if release_over_group and not release_id:
                                        release_id = album_music_ids.get('MusicBrainzAlbum')
                            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
                                logger.debug(
                                    f'Failed to Fetch Album Details ({album_id})', exc_info=True
                                )

                        if not group_id and release_id:
                            group_id = await get_music_id_from_release(cache_session, release_id)
                        if not group_id and musicbrainz_search:
                            logger.warning('No MusicBrainz ID Found. Searching...')
                            if 'AlbumArtist' in media_dict and 'Album' in media_dict:
                                group_id = await get_music_id_from_search(
                                    cache_session, media_dict['AlbumArtist'], media_dict['Album']
                                )

                        if not always_use_coverart and album_id and is_https:
                            poster_url = f'{jf_host}/Items/{album_id}/Images/Primary/0'
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

                    elif media_type == 'TvChannel':
                        program_dict = cached_program_dict
                        season = program_dict.get('ParentIndexNumber')
                        episode = program_dict.get('IndexNumber')
                        series_name = sanitize_broadcast_title(
                            program_dict.get('SeriesName') or program_dict.get('Name')
                        )

                        program_ids = program_dict.get('ProviderIds', {})
                        tmdb_id = program_ids.get('Tmdb') or program_ids.get('TheMovieDb')
                        is_movie = program_dict.get('IsMovie')
                        is_series_match, series_tmdb_id = False, None

                        if tmdb_api_key and tmdb_id:
                            if not is_movie:
                                series_tmdb_id = tmdb_id
                                is_series_match = True
                        elif tmdb_api_key and series_name:
                            logger.warning('No TMDB ID Found. Searching...')
                            if is_movie:
                                tmdb_id = await get_movie_id(
                                    cache_session, tmdb_api_key, series_name
                                )
                            else:
                                series_tmdb_id = await get_series_id(
                                    cache_session, tmdb_api_key, series_name
                                )
                                if series_tmdb_id:
                                    is_series_match = True
                                else:
                                    tmdb_id = await get_movie_id(
                                        cache_session, tmdb_api_key, series_name
                                    )

                        if not always_use_tmdb and is_https:
                            program_id = program_dict.get('Id')
                            if program_id and program_dict.get('ImageTags', {}).get('Primary'):
                                poster_url = f'{jf_host}/Items/{program_id}/Images/Primary/0'
                            elif media_id:
                                poster_url = f'{jf_host}/Items/{media_id}/Images/Primary/0'

                        if poster_url == 'large_image' and tmdb_api_key:
                            if is_series_match and series_tmdb_id:
                                if season_over_series and season is not None:
                                    poster_url = await get_season_poster(
                                        cache_session,
                                        tmdb_api_key,
                                        series_tmdb_id,
                                        languages,
                                        season,
                                    )
                                else:
                                    poster_url = await get_series_poster(
                                        cache_session, tmdb_api_key, series_tmdb_id, languages
                                    )
                            elif tmdb_id:
                                poster_url = await get_movie_poster(
                                    cache_session, tmdb_api_key, tmdb_id, languages
                                )

                        if is_series_match and series_tmdb_id:
                            details_url = f'https://www.themoviedb.org/tv/{series_tmdb_id}'
                            if season is not None:
                                if episode is not None:
                                    state_url = f'{details_url}/season/{season}/episode/{episode}'
                                else:
                                    state_url = f'{details_url}/season/{season}'
                        elif tmdb_id:
                            details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'

                    display_name = server_name
                    if (
                        show_server_name
                        and server_name is not None
                        and activity_type == ActivityType.WATCHING
                    ):
                        display_name = f'on {server_name}'

                    cached_media_id = media_id
                    cached_kwargs = {
                        'activity_type': activity_type,
                        'status_display_type': StatusDisplayType.DETAILS,
                        'name': display_name,
                        'details': sanitize_discord_str(details_str),
                        'details_url': details_url,
                        'state': sanitize_discord_str(state_str),
                        'state_url': state_url,
                        'large_image': poster_url,
                    }

                except KeyError as e:
                    missing_key = str(e).strip('\'"')
                    if missing_key not in missing_session_keys:
                        logger.warning(f'Missing Key in Session Data: {e}')
                        logger.debug(type(e).__name__, exc_info=True)
                        missing_session_keys.add(missing_key)
                    await asyncio.sleep(polling_rate)
                    continue
                else:
                    missing_session_keys = set()

            if media_changed or position_acquired or seek_detected or playstate_changed:
                rpc_state.has_pending_update = True
                if media_changed or position_acquired:
                    rpc_state.pending_payload = None
                elif seek_detected:
                    delta_str = f'+{seek_delta}s' if seek_delta > 0 else f'{seek_delta}s'
                    rpc_state.pending_payload = ('seek_detected', f'Seek Detected ({delta_str})')
                elif playstate_changed:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    rpc_state.pending_payload = ('playstate_changed', f'PlayState {playstate}')

            remaining_cooldown = polling_rate - (time.time() - rpc_state.last_update_time)
            if rpc_state.has_pending_update and remaining_cooldown <= 0:
                if not cached_kwargs:
                    rpc_state.has_pending_update = False
                    rpc_state.pending_payload = None
                    continue

                small_image = (
                    'media_paused' if session_paused else 'small_image' if show_jf_logo else None
                )

                if rpc_state.last_activity_str != activity_str:
                    logger.info(f'"{activity_str}"')
                if rpc_state.pending_payload:
                    _, payload = rpc_state.pending_payload
                    logger.log(15, payload)
                    rpc_state.pending_payload = None

                try:
                    await discord_rpc.update(
                        **cached_kwargs,
                        start=current_start,
                        end=current_end,
                        small_image=small_image,
                    )
                    rpc_state.last_update_time = time.time()
                    rpc_state.last_activity_str = activity_str
                    rpc_state.last_paused_state = session_paused
                    rpc_state.has_pending_update = False
                except (PyPresenceException, OSError, KeyError) as e:
                    logger.error(f'RPC Update Error: {type(e).__name__}')
                    logger.debug(type(e).__name__, exc_info=True)
                    await await_discord_connect(discord_rpc, polling_rate)
                    await asyncio.sleep(polling_rate)
                    continue

        elif rpc_state.last_activity_str:
            if rpc_state.last_packet_time is None or time.time() >= (
                rpc_state.last_packet_time + session_timeout
            ):
                if (time.time() - rpc_state.last_update_time) < polling_rate:
                    continue
                if not await clear_activity(discord_rpc, polling_rate):
                    continue
                rpc_state.reset()
                cached_media_id = None
                cached_kwargs.clear()

        if not ws_state.get('ws_connected'):
            await asyncio.sleep(polling_rate)


async def monitor_activity(
    config: SectionProxy,
    ini_path: str,
    polling_rate: int,
    seek_threshold: int,
    session_timeout: int,
    stale_grace_period: int,
) -> None:
    client_id = config.get('DISCORD_CLIENT_ID', CLIENT_ID)
    discord_rpc = AioPresence(client_id)
    await await_discord_connect(discord_rpc, polling_rate)

    timeout = aiohttp.ClientTimeout(5.0)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    jf_connector = aiohttp.TCPConnector(ssl=ssl_context)
    cache_connector = aiohttp.TCPConnector(ssl=ssl_context)

    ws_state: dict[str, Any] = {'sessions': [], 'ws_connected': False}
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
                jf_host = await resolve_host_url(jf_session, jf_host)
                config['JELLYFIN_HOST'] = jf_host
                logger.warning(f'Missing URL Protocol: {jf_host}')

            ws_task = asyncio.create_task(
                websocket_listener(jf_session, config, polling_rate, ws_state, wake_event)
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
                    session_timeout,
                    stale_grace_period,
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
        await asyncio.sleep(0.25)


def start_discord_rpc(
    ini_path: str,
    log_path: str | None = None,
    log_queue: queue.Queue[LogRecord] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    config = load_config(ini_path)
    polling_rate = max(1, config.getint('POLLING_RATE', 5))
    seek_threshold = max(1, config.getint('SEEK_THRESHOLD', 10))
    session_timeout = max(1, config.getint('SESSION_TIMEOUT', 2))
    stale_grace_period = max(1, config.getint('STALE_GRACE_PERIOD', 5))

    logger.setLevel(logging.DEBUG)
    log_level = get_valid_level(config.get('LOG_LEVEL', ''), logging.INFO)
    log_level_file = get_valid_level(config.get('LOG_LEVEL_FILE', ''), logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    if log_path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        max_bytes = int(config.get('LOG_MAX_BYTES', 5242880))
        max_files = int(config.get('LOG_MAX_FILES', 3))
        file_hdlr = handlers.RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=max_files, encoding='utf-8'
        )
        file_hdlr.setFormatter(formatter)
        file_hdlr.setLevel(log_level_file)
        logger.addHandler(file_hdlr)

    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    stream_hdlr.setLevel(log_level)
    logger.addHandler(stream_hdlr)

    if log_queue is not None:
        queue_hdlr = handlers.QueueHandler(log_queue)
        queue_hdlr.setLevel(log_level)
        logger.addHandler(queue_hdlr)

    async def runner() -> None:
        main_task = asyncio.create_task(
            monitor_activity(
                config, ini_path, polling_rate, seek_threshold, session_timeout, stale_grace_period
            )
        )
        watcher_task = None
        if stop_event is not None:

            async def stop_watcher(event: threading.Event) -> None:
                while not event.is_set():
                    await asyncio.sleep(0.2)
                main_task.cancel()

            watcher_task = asyncio.create_task(stop_watcher(stop_event))

        try:
            await main_task
        except asyncio.CancelledError:
            pass
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher_task

    asyncio.run(runner())


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
