import argparse
import asyncio
import logging
import re
import signal
import sys
import time
from configparser import ConfigParser, SectionProxy
from email.utils import parseaddr
from importlib.metadata import metadata
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from types import FrameType
from typing import Any

import aiohttp
from aiohttp import ClientSession
from aiohttp_client_cache import CacheBackend
from aiohttp_client_cache.session import CachedSession
from pypresence.exceptions import PyPresenceException
from pypresence.presence import AioPresence
from pypresence.types import ActivityType, StatusDisplayType

CLIENT_ID = '1238889120672120853'
logger = logging.getLogger('RPC')

pkg_metadata = metadata('jellyfin-rpc')
contact_info = parseaddr(pkg_metadata['Author-email'])[1]
USER_AGENT = f'Jellyfin-RPC/{pkg_metadata["Version"]} ( {contact_info} )'


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


def get_delimited_list(config: SectionProxy, option: str) -> list[str]:
    option_split = re.split(r'[,;|]', config.get(option, ''))
    return [x.strip() for x in option_split if x.strip()]


async def get_jf_user_and_server(
    session: ClientSession, config: SectionProxy, show_server_name: bool, polling_rate: int
) -> tuple[str, str | None]:
    try:
        jf_host = config['JELLYFIN_HOST'].rstrip('/')
        jf_username = config['JELLYFIN_USERNAME']
        jf_api_key = config['JELLYFIN_API_KEY']
    except KeyError as e:
        logger.error(f'Missing Key in INI Config: {e}')
        sys.exit(0)

    initial_attempt = True
    headers = {'Accept': 'application/json', 'X-Emby-Token': jf_api_key}
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
                logger.error(f'Username Not Found: {jf_username}')
                sys.exit(0)

            server_name = None
            if show_server_name:
                async with session.get(f'{jf_host}/System/Info', headers=headers) as response:
                    response.raise_for_status()
                    system_info = await response.json()
                    server_name = system_info.get('ServerName', 'Jellyfin')

            logger.info('Connected to Jellyfin API')
            return user_id, server_name

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)


async def get_series_id(session: ClientSession, api_key: str, title: str) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/tv'
    search_params = {'api_key': api_key, 'query': title}
    try:
        async with session.get(search_url, params=search_params) as response:
            response.raise_for_status()
            data = await response.json()
            return data['results'][0]['id']
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'TMDB API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return None


async def get_movie_id(session: ClientSession, api_key: str, title: str) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/movie'
    search_params = {'api_key': api_key, 'query': title}
    try:
        async with session.get(search_url, params=search_params) as response:
            response.raise_for_status()
            data = await response.json()
            return data['results'][0]['id']
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f'TMDB API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
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
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f'MusicBrainz API Network Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    except (ValueError, KeyError, IndexError) as e:
        logger.warning(f'MusicBrainz API Parsing Error ({type(e).__name__}). Skipping...')
        logger.debug(e)
    return None


def select_poster(posters: list[dict[str, Any]], languages: list[str]) -> dict[str, Any]:
    def get_poster_score(poster: dict[str, Any]) -> tuple[float, int]:
        return float(poster.get('vote_average', 0.0)), int(poster.get('vote_count', 0))

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
    images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/images'
    images_params = {'api_key': api_key}
    try:
        async with session.get(images_url, params=images_params) as response:
            response.raise_for_status()
            data = await response.json()
            if poster := select_poster(data['posters'], languages):
                return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
            logger.warning('No Poster Available on TMDB. Skipping...')
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
    images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images'
    images_params = {'api_key': api_key}
    try:
        async with session.get(images_url, params=images_params) as response:
            response.raise_for_status()
            data = await response.json()
            poster = select_poster(data['posters'], languages)
            return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
    except aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError:
        return await get_series_poster(session, api_key, tmdb_id, languages)


async def get_movie_poster(
    session: ClientSession, api_key: str, tmdb_id: str, languages: list[str]
) -> str:
    images_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}/images'
    images_params = {'api_key': api_key}
    try:
        async with session.get(images_url, params=images_params) as response:
            response.raise_for_status()
            data = await response.json()
            if poster := select_poster(data['posters'], languages):
                return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
            logger.warning('No Poster Available on TMDB. Skipping...')
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
    except aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError:
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


async def activity_loop(
    jf_session: ClientSession,
    cache_session: ClientSession,
    discord_rpc: AioPresence,
    config: SectionProxy,
    polling_rate: int,
    seek_threshold: int,
) -> None:
    jf_host = config['JELLYFIN_HOST'].rstrip('/')
    jf_api_key = config['JELLYFIN_API_KEY']
    jf_username = config['JELLYFIN_USERNAME']
    jf_headers = {'Accept': 'application/json', 'X-Emby-Token': jf_api_key}

    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_server_name = config.getboolean('SHOW_SERVER_NAME', False)
    show_jf_icon = config.getboolean('SHOW_JELLYFIN_ICON', False)

    user_id, server_name = await get_jf_user_and_server(
        jf_session, config, show_server_name, polling_rate
    )

    if tmdb_api_key := config.get('TMDB_API_KEY'):
        await check_tmdb_connection(cache_session, tmdb_api_key)

    languages = get_delimited_list(config, 'POSTER_LANGUAGES')
    for lang in languages:
        if len(lang) != 2 or not lang.isalpha():
            logger.warning(f'Invalid ISO 639-1 Language "{lang}"')
    if config.getboolean('TEXTLESS_POSTERS', True):
        languages.insert(0, '')

    always_use_tmdb = config.getboolean('ALWAYS_USE_TMDB', False)
    season_over_series = config.getboolean('SEASON_OVER_SERIES', True)

    always_use_musicbrainz = config.getboolean('ALWAYS_USE_MUSICBRAINZ', False)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', True)

    whitelist = get_delimited_list(config, 'WHITELIST_LIBRARIES')
    blacklist = get_delimited_list(config, 'BLACKLIST_LIBRARIES')

    media_types = get_delimited_list(config, 'MEDIA_TYPES')
    jf_media_types = set()
    if 'Shows' in media_types:
        jf_media_types.add('Episode')
    if 'Movies' in media_types:
        jf_media_types.add('Movie')
    if 'Music' in media_types:
        jf_media_types.add('Audio')

    activity = previous_activity = previous_start = None
    previous_warning = previous_playstate = False
    cached_item_id = cached_library = None
    cached_kwargs: dict[str, Any] = {}

    while True:
        try:
            async with jf_session.get(f'{jf_host}/Sessions', headers=jf_headers) as response:
                response.raise_for_status()
                sessions = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f'Session Polling Error: {e}')
            user_id, server_name = await get_jf_user_and_server(
                jf_session, config, show_server_name, polling_rate
            )
            await asyncio.sleep(polling_rate)
            continue
        except ValueError as e:
            logger.debug(f'Session Parsing Error: {e}')
            await asyncio.sleep(polling_rate)
            continue

        user_sessions = [s for s in sessions if s.get('UserName') == jf_username]
        if not user_sessions:
            await asyncio.sleep(polling_rate)
            continue

        session_data: dict[str, Any] = {}
        for user_session in user_sessions:
            if not (item := user_session.get('NowPlayingItem')):
                continue
            media_type = item.get('Type')
            if media_type in jf_media_types:
                session_data = user_session
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
                    except (PyPresenceException, OSError, KeyError) as e:
                        logger.debug(f'Activity Clear Error: {e}')
                        await await_connection(discord_rpc, polling_rate)
                        await asyncio.sleep(polling_rate)
                        continue
                    logger.info('Activity Cleared')
                    previous_activity = previous_start = None
                    previous_playstate = False
                await asyncio.sleep(polling_rate)
                continue

            try:
                state = details = None
                media_dict = session_data['NowPlayingItem']
                item_id = media_dict.get('Id')

                if whitelist or blacklist:
                    library = None
                    if item_id == cached_item_id:
                        library = cached_library
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
                                    library = ancestor.get('Name')
                                    break
                            if library:
                                cached_item_id, cached_library = item_id, library
                        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                            logger.error('Library Retrieval Failed. Skipping...')
                            logger.debug(e)

                    is_allowed = True
                    if library:
                        if whitelist and library not in whitelist:
                            is_allowed = False
                        if blacklist and library in blacklist:
                            is_allowed = False
                    elif whitelist:
                        is_allowed = False

                    if not is_allowed:
                        if previous_activity is not None:
                            try:
                                await discord_rpc.clear()
                            except (PyPresenceException, OSError, KeyError) as e:
                                logger.debug(f'Activity Clear Error: {e}')
                                await await_connection(discord_rpc, polling_rate)
                                await asyncio.sleep(polling_rate)
                                continue
                            logger.info('Activity Cleared (Library Blocked)')
                            previous_activity = previous_start = None
                            previous_playstate = False
                        await asyncio.sleep(polling_rate)
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
                        if 'Artists' in media_dict and media_dict['Artists']:
                            state = ', '.join(media_dict['Artists'])
                        if 'Album' in media_dict and media_dict['Album']:
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
                            except (PyPresenceException, OSError, KeyError) as e:
                                logger.debug(f'Activity Clear Error: {e}')
                                await await_connection(discord_rpc, polling_rate)
                                await asyncio.sleep(polling_rate)
                                continue
                            logger.info('Activity Cleared (Unsupported Media)')
                            previous_activity = previous_start = None
                            previous_playstate = False
                        await asyncio.sleep(polling_rate)
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

            current_start = current_end = None
            if not session_paused:
                try:
                    position_ticks = int(session_data['PlayState']['PositionTicks'])
                    current_start = int(time.time() - position_ticks / 10_000_000)
                    runtime_ticks = int(media_dict['RunTimeTicks'])
                    current_end = int(current_start + runtime_ticks / 10_000_000)
                except KeyError, TypeError, ValueError:
                    pass

            media_changed = previous_activity != activity
            playstate_changed = previous_playstate != session_paused

            seek_detected = False
            if not session_paused and previous_start is not None and current_start is not None:
                if abs(current_start - previous_start) > seek_threshold:
                    seek_detected = True

            if media_changed:
                poster_url = 'large_image'
                state_url = large_url = details_url = None

                is_https = jf_host.startswith('https://')

                if media_type == 'Episode':
                    tmdb_id = None
                    if jf_series_id := media_dict.get('SeriesId'):
                        try:
                            async with jf_session.get(
                                f'{jf_host}/Items/{jf_series_id}',
                                headers=jf_headers,
                                params={'userId': user_id},
                            ) as response:
                                response.raise_for_status()
                                series_item = await response.json()
                                series_ids = series_item.get('ProviderIds', {})
                                tmdb_id = series_ids.get('Tmdb') or series_ids.get('TheMovieDb')
                        except aiohttp.ClientError, asyncio.TimeoutError, ValueError:
                            pass

                    if not tmdb_id and tmdb_api_key:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'SeriesName' in media_dict:
                            tmdb_id = await get_series_id(
                                cache_session, tmdb_api_key, media_dict['SeriesName']
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
                    tmdb_id = movie_ids.get('Tmdb') or movie_ids.get('TheMovieDb')

                    if not tmdb_id and tmdb_api_key:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'Name' in media_dict:
                            tmdb_id = await get_movie_id(
                                cache_session, tmdb_api_key, media_dict['Name']
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
                        except aiohttp.ClientError, asyncio.TimeoutError, ValueError:
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
                                except aiohttp.ClientError, asyncio.TimeoutError, ValueError:
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

            if media_changed or playstate_changed or seek_detected:
                small_image = 'small_image' if show_jf_icon else None
                try:
                    await discord_rpc.update(
                        **cached_kwargs,
                        start=current_start,
                        end=current_end,
                        small_image=small_image,
                    )
                except (PyPresenceException, OSError, KeyError) as e:
                    logger.debug(f'Activity Update Error: {e}')
                    await await_connection(discord_rpc, polling_rate)
                    await asyncio.sleep(polling_rate)
                    continue

                if media_changed:
                    logger.info(f'"{activity}"')
                elif playstate_changed:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    logger.debug(f'PlayState {playstate}')
                elif seek_detected:
                    logger.debug('Seek Detected')

                previous_activity, previous_playstate = activity, session_paused
                previous_start = current_start

        elif previous_activity is not None:
            try:
                await discord_rpc.clear()
            except (PyPresenceException, OSError, KeyError) as e:
                logger.debug(f'Activity Clear Error: {e}')
                await await_connection(discord_rpc, polling_rate)
                await asyncio.sleep(polling_rate)
                continue
            logger.info('Activity Cleared')
            previous_activity = previous_start = None
            previous_playstate = False

        await asyncio.sleep(polling_rate)


async def monitor_activity(config: SectionProxy, polling_rate: int, seek_threshold: int) -> None:
    client_id = config.get('DISCORD_CLIENT_ID', CLIENT_ID)
    discord_rpc = AioPresence(client_id)
    await await_connection(discord_rpc, polling_rate)

    timeout = aiohttp.ClientTimeout(5.0)
    jf_connector, cache_connector = aiohttp.TCPConnector(), aiohttp.TCPConnector()
    async with (
        ClientSession(timeout=timeout, connector=jf_connector) as jf_session,
        CachedSession(
            cache=CacheBackend(), timeout=timeout, connector=cache_connector
        ) as cache_session,
    ):
        await activity_loop(
            jf_session, cache_session, discord_rpc, config, polling_rate, seek_threshold
        )


def start_discord_rpc(
    ini_path: str, log_path: str | None = None, log_queue: Queue[LogRecord] | None = None
) -> None:
    config = load_config(ini_path)
    polling_rate = max(1, config.getint('POLLING_RATE', config.getint('REFRESH_RATE', 5)))
    seek_threshold = max(1, config.getint('SEEK_THRESHOLD', 10))

    log_level = config.get('LOG_LEVEL', 'INFO').upper()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    logger.setLevel(logging.DEBUG)

    if log_path is not None:
        file_hdlr = logging.FileHandler(log_path, encoding='utf-8')
        file_hdlr.setFormatter(formatter)
        file_hdlr.setLevel(logging.DEBUG)
        logger.addHandler(file_hdlr)

    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    stream_hdlr.setLevel(log_level)
    logger.addHandler(stream_hdlr)

    if log_queue is not None:
        queue_hdlr = handlers.QueueHandler(log_queue)
        queue_hdlr.setLevel(log_level)
        logger.addHandler(queue_hdlr)

    def handle_shutdown(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        asyncio.run(monitor_activity(config, polling_rate, seek_threshold))
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-path', type=str, required=True)
    parser.add_argument('--log-path', type=str)
    args = parser.parse_args()

    start_discord_rpc(args.ini_path, args.log_path)


if __name__ == '__main__':
    main()
