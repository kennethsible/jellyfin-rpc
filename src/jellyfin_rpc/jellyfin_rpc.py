import argparse
import asyncio
import json
import logging
import re
import signal
import sys
import time
import uuid
from configparser import ConfigParser, SectionProxy
from email.utils import parseaddr
from importlib.metadata import metadata
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from types import FrameType
from typing import Any, cast

import requests
from jellyfin_apiclient_python import JellyfinClient, api
from jellyfin_apiclient_python.exceptions import HTTPException
from pypresence.exceptions import PyPresenceException
from pypresence.presence import AioPresence
from pypresence.types import ActivityType, StatusDisplayType
from requests.exceptions import RequestException

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


async def get_jf_api(config: SectionProxy, refresh_rate: int) -> tuple[api.API, str | None]:
    initial_attempt = True
    while True:
        try:
            url = config['JELLYFIN_HOST'] + '/Users'
            headers = {'Accept': 'application/json', 'X-Emby-Token': config['JELLYFIN_API_KEY']}
            user_data = requests.get(url, headers=headers, verify=True)
            user_data.raise_for_status()

            user_id = None
            for user in user_data.json():
                if config['JELLYFIN_USERNAME'] in user['Name']:
                    user_id = user['Id']
            if user_id is None:
                logger.error(f'Username Not Found: {config["JELLYFIN_USERNAME"]}')
                sys.exit(0)

            client = JellyfinClient()
            client.config.app('jellyfin-rpc', '0.1.0', 'Discord RPC', uuid.uuid4())
            client.config.data['auth.ssl'] = True
            client.authenticate(
                {
                    'Servers': [
                        {
                            'address': config['JELLYFIN_HOST'],
                            'AccessToken': config['JELLYFIN_API_KEY'],
                            'UserId': user_id,
                            'DateLastAccessed': 0,
                        }
                    ]
                },
                discover=False,
            )

            server_name = None
            if config.getboolean('SHOW_SERVER_NAME', False):
                server_name = client.jellyfin.get_system_info().get('ServerName')
            logger.info('Connected to Jellyfin API')
        except (RequestException, JSONDecodeError, HTTPException) as e:
            if initial_attempt:
                logger.debug(e)
                logger.error('Jellyfin API Connection Failed. Retrying...')
            initial_attempt = False
            await asyncio.sleep(refresh_rate)
            continue
        except KeyError as e:
            logger.error(f'Missing Key in INI Config: {e}')
            sys.exit(0)
        return client.jellyfin, server_name


def check_tmdb_api(api_key: str) -> None:
    config_url = 'https://api.themoviedb.org/3/configuration'
    config_params = {'api_key': api_key}
    try:
        response = requests.get(config_url, params=config_params)
        response.raise_for_status()
        logger.info('Connected to TMDB API')
    except RequestException as e:
        logger.debug(e)
        logger.warning('TMDB API Connection Failed. Skipping...')


def get_series_id(api_key: str, title: str) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/tv'
    search_params = {'api_key': api_key, 'query': title}
    try:
        response = requests.get(search_url, params=search_params)
        response.raise_for_status()
        return cast(str, response.json()['results'][0]['id'])
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('TMDB API Connection Failed. Skipping...')
    return None


def get_movie_id(api_key: str, title: str) -> str | None:
    search_url = 'https://api.themoviedb.org/3/search/movie'
    search_params = {'api_key': api_key, 'query': title}
    try:
        response = requests.get(search_url, params=search_params)
        response.raise_for_status()
        return cast(str, response.json()['results'][0]['id'])
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('TMDB API Connection Failed. Skipping...')
    return None


def get_music_id(artist: str, album: str) -> str | None:
    artist, album = artist.lower(), album.lower()
    search_url = 'https://musicbrainz.org/ws/2/release-group'
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    artist_query = f'artist:({artist}) OR artistalias:({artist})'
    album_query = f'releasegroup:({album}) OR alias:({album}'
    params = {'query': f'({artist_query}) AND ({album_query})', 'fmt': 'json'}
    try:
        response = requests.get(search_url, headers=headers, params=params)
        response.raise_for_status()
        return cast(str, response.json()['release-groups'][0]['id'])
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('MusicBrainz API Connection Failed. Skipping...')
    return None


def select_poster(posters: list[dict[str, str]], languages: list[str]) -> dict[str, str]:
    matched_posters: list[dict[str, str]] = []
    for lang_code in languages:
        for poster in posters:
            if poster.get('iso_639_1') == (lang_code or None):
                matched_posters.append(poster)
        if matched_posters:
            break
    matched_posters.sort(key=lambda x: x['vote_count'], reverse=True)
    return matched_posters[0] if matched_posters else posters[0]


def get_series_poster(api_key: str, tmdb_id: str, languages: list[str]) -> str:
    images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/images'
    images_params = {'api_key': api_key}
    try:
        response = requests.get(images_url, params=images_params)
        response.raise_for_status()
        poster = select_poster(json.loads(response.text)['posters'], languages)
        return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Poster Available on TMDB. Skipping...')
        return 'large_image'


def get_season_poster(
    api_key: str, tmdb_id: str, languages: list[str], season: int | None = None
) -> str:
    if season is None:
        return get_series_poster(api_key, tmdb_id, languages)
    images_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images'
    images_params = {'api_key': api_key}
    try:
        response = requests.get(images_url, params=images_params)
        response.raise_for_status()
        poster = select_poster(json.loads(response.text)['posters'], languages)
        return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
    except (RequestException, JSONDecodeError, KeyError, IndexError):
        return get_series_poster(api_key, tmdb_id, languages)


def get_movie_poster(api_key: str, tmdb_id: str, languages: list[str]) -> str:
    images_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}/images'
    images_params = {'api_key': api_key}
    try:
        response = requests.get(images_url, params=images_params)
        response.raise_for_status()
        poster = select_poster(json.loads(response.text)['posters'], languages)
        return 'https://image.tmdb.org/t/p/w185/' + poster['file_path']
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Poster Available on TMDB. Skipping...')
        return 'large_image'


def get_release_group_cover(group_id: str) -> str:
    try:
        response = requests.get(f'https://coverartarchive.org/release-group/{group_id}')
        response.raise_for_status()
        return cast(str, json.loads(response.text)['images'][0]['image'])
    except (RequestException, JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Cover Art Available on MusicBrainz. Skipping...')
    return 'large_image'


def get_release_cover(group_id: str, release_id: str | None = None) -> str:
    if not release_id:
        return get_release_group_cover(group_id)
    try:
        response = requests.get(f'https://coverartarchive.org/release/{release_id}')
        response.raise_for_status()
        return cast(str, json.loads(response.text)['images'][0]['image'])
    except (RequestException, JSONDecodeError, KeyError, IndexError):
        return get_release_group_cover(group_id)


async def await_connection(discord_rpc: AioPresence, refresh_rate: int) -> None:
    initial_attempt = True
    while True:
        try:
            await discord_rpc.connect()
            logger.info('Connected to Discord Client')
        except (PyPresenceException, ConnectionRefusedError) as e:
            if initial_attempt:
                logger.debug(e)
                logger.error('Discord Client Connection Failed. Retrying...')
            initial_attempt = False
            await asyncio.sleep(refresh_rate)
            continue
        break


async def monitor_activity(config: SectionProxy, refresh_rate: int) -> None:
    client_id = config.get('DISCORD_CLIENT_ID', CLIENT_ID)
    discord_rpc = AioPresence(client_id)
    await await_connection(discord_rpc, refresh_rate)

    jf_api, server_name = await get_jf_api(config, refresh_rate)

    season_over_series = config.getboolean('SEASON_OVER_SERIES', True)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', True)
    find_best_match = config.getboolean('FIND_BEST_MATCH', True)
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_jf_icon = config.getboolean('SHOW_JELLYFIN_ICON', False)
    languages = re.split(r'[\s,]+', config.get('POSTER_LANGUAGES', ''))

    if config.get('TMDB_API_KEY'):
        check_tmdb_api(config['TMDB_API_KEY'])

    activity = previous_activity = None
    previous_warning = previous_playstate = False
    cached_kwargs: dict[str, Any] = {}
    while True:
        try:
            session = next(
                session
                for session in jf_api.sessions()
                if config['JELLYFIN_USERNAME'] == session['UserName']
            )
        except (StopIteration, KeyError):
            await asyncio.sleep(refresh_rate)
            continue
        except (RequestException, JSONDecodeError, HTTPException) as e:
            logger.debug(e)
            jf_api, server_name = await get_jf_api(config, refresh_rate)
            await asyncio.sleep(refresh_rate)
            continue

        if 'NowPlayingItem' in session:
            try:
                session_paused = session['PlayState']['IsPaused']
            except KeyError as e:
                logger.warning(f'Missing Key in Session Data: {e}')
                session_paused = False

            if session_paused and not show_when_paused:
                if previous_activity is not None:
                    try:
                        await discord_rpc.clear()
                    except (PyPresenceException, ConnectionRefusedError) as e:
                        logger.debug(e)
                        await await_connection(discord_rpc, refresh_rate)
                        await asyncio.sleep(refresh_rate)
                        continue
                    logger.info('Activity Cleared')
                    previous_activity, previous_playstate = None, False
                await asyncio.sleep(refresh_rate)
                continue

            try:
                state = details = None
                media_dict = session['NowPlayingItem']
                media_types = config['MEDIA_TYPES'].split(',')
                match media_type := media_dict['Type']:
                    case 'Episode':
                        activity_type = ActivityType.WATCHING
                        if 'Shows' not in media_types:
                            await asyncio.sleep(refresh_rate)
                            continue
                        season = media_dict['ParentIndexNumber']
                        episode = media_dict['IndexNumber']
                        details = media_dict['SeriesName']
                        state = f'{f"S{season}:E{episode}"} - {media_dict["Name"]}'
                        activity = f'{details} {state.split(" - ")[0]}'
                    case 'Movie':
                        activity_type = ActivityType.WATCHING
                        if 'Movies' not in media_types:
                            await asyncio.sleep(refresh_rate)
                            continue
                        details = media_dict['Name']
                        activity = details
                    case 'Audio':
                        activity_type = ActivityType.LISTENING
                        if 'Music' not in media_types:
                            await asyncio.sleep(refresh_rate)
                            continue
                        if 'Artists' in media_dict and media_dict['Artists']:
                            state = ', '.join(media_dict['Artists'])
                        if 'Album' in media_dict and media_dict['Album']:
                            if state:
                                state += f' - {media_dict["Album"]}'
                            else:
                                state = media_dict['Album']
                        details = media_dict['Name']
                        activity = details
                        if state:
                            activity += f' - {state.split(" - ")[0]}'
                    case _:
                        if not previous_warning:
                            logger.warning(f'Unsupported Media Type "{media_type}". Skipping...')
                            previous_warning = True
                        await asyncio.sleep(refresh_rate)
                        continue  # raise NotImplementedError()
                if len(details) < 2:  # e.g., Chinese characters
                    details += ' '
            except KeyError as e:
                if not previous_warning:
                    logger.warning(f'Missing Key in Session Data: {e}. Skipping...')
                    previous_warning = True
                await asyncio.sleep(refresh_rate)
                continue
            previous_warning = False

            if media_changed := previous_activity != activity:
                poster_url = 'large_image'
                state_url = large_url = details_url = None

                if media_type == 'Episode' and config.get('TMDB_API_KEY'):
                    tmdb_id = None
                    if 'SeriesId' in media_dict:
                        try:
                            series_item = jf_api.get_item(media_dict['SeriesId'])
                            series_ids = series_item.get('ProviderIds', {})
                            tmdb_id = series_ids.get('Tmdb') or series_ids.get('TheMovieDb')
                        except (RequestException, JSONDecodeError, HTTPException):
                            pass

                    if not tmdb_id and find_best_match:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'SeriesName' in media_dict:
                            tmdb_id = get_series_id(
                                config['TMDB_API_KEY'], media_dict['SeriesName']
                            )
                        if not tmdb_id:
                            logger.warning('TMDB ID Search Failed. Skipping...')
                    elif not tmdb_id:
                        logger.warning('No TMDB ID Found. Skipping...')

                    if tmdb_id:
                        details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                        season = media_dict['ParentIndexNumber']
                        if season_over_series:
                            poster_url = get_season_poster(
                                config['TMDB_API_KEY'], tmdb_id, languages, season
                            )
                        if 'IndexNumber' in media_dict:
                            episode = media_dict['IndexNumber']
                            state_url = f'{details_url}/season/{season}/episode/{episode}'
                        large_url = f'{details_url}/season/{season}'

                elif media_type == 'Movie' and config.get('TMDB_API_KEY'):
                    movie_ids = media_dict.get('ProviderIds', {})
                    tmdb_id = movie_ids.get('Tmdb') or movie_ids.get('TheMovieDb')

                    if not tmdb_id and find_best_match:
                        logger.warning('No TMDB ID Found. Searching...')
                        if 'Name' in media_dict:
                            tmdb_id = get_movie_id(config['TMDB_API_KEY'], media_dict['Name'])
                        if not tmdb_id:
                            logger.warning('TMDB ID Search Failed. Skipping...')
                    elif not tmdb_id:
                        logger.warning('No TMDB ID Found. Skipping...')

                    if tmdb_id:
                        poster_url = get_movie_poster(config['TMDB_API_KEY'], tmdb_id, languages)
                        details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'
                        large_url = details_url

                elif media_type == 'Audio':
                    music_ids = media_dict.get('ProviderIds', {})
                    group_id = music_ids.get('MusicBrainzReleaseGroup')

                    album_item = None
                    if not group_id and 'AlbumId' in media_dict:
                        try:
                            album_item = jf_api.get_item(media_dict['AlbumId'])
                            album_music_ids = album_item.get('ProviderIds', {})
                            group_id = album_music_ids.get('MusicBrainzReleaseGroup')
                        except (RequestException, JSONDecodeError, HTTPException):
                            pass

                    if not group_id and find_best_match:
                        logger.warning('No MusicBrainz ID Found. Searching...')
                        if 'AlbumArtist' in media_dict and 'Album' in media_dict:
                            group_id = get_music_id(media_dict['AlbumArtist'], media_dict['Album'])
                        if not group_id:
                            logger.warning('MusicBrainz ID Search Failed. Skipping...')
                    elif not group_id:
                        logger.warning('No MusicBrainz ID Found. Skipping...')

                    if group_id:
                        release_id = None
                        if release_over_group:
                            release_id = music_ids.get('MusicBrainzAlbum')
                            if not release_id and 'AlbumId' in media_dict:
                                try:
                                    if album_item is None:
                                        album_item = jf_api.get_item(media_dict['AlbumId'])
                                    album_music_ids = album_item.get('ProviderIds', {})
                                    release_id = album_music_ids.get('MusicBrainzAlbum')
                                except (RequestException, JSONDecodeError, HTTPException):
                                    pass

                        poster_url = get_release_cover(group_id, release_id)
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
                    'state': state,
                    'state_url': state_url,
                    'details': details,
                    'details_url': details_url,
                    'name': server_name,
                    'large_image': poster_url,
                    'large_url': large_url,
                }

            playstate_changed = previous_playstate != session_paused
            if media_changed or playstate_changed:
                start_time, end_time = None, None
                if not session_paused:
                    try:
                        position_ticks = int(session['PlayState']['PositionTicks'])
                        start_time = int(time.time() - position_ticks / 10_000_000)
                        runtime_ticks = int(media_dict['RunTimeTicks'])
                        end_time = int(start_time + runtime_ticks / 10_000_000)
                    except (KeyError, TypeError, ValueError):
                        pass
                small_image = 'small_image' if show_jf_icon else None
                try:
                    await discord_rpc.update(
                        **cached_kwargs, start=start_time, end=end_time, small_image=small_image
                    )
                except (PyPresenceException, ConnectionRefusedError) as e:
                    logger.debug(e)
                    await await_connection(discord_rpc, refresh_rate)
                    await asyncio.sleep(refresh_rate)
                    continue

                if previous_activity is None or previous_activity != activity:
                    logger.info(f'Activity Updated "{activity}"')
                else:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    logger.debug(f'PlayState Changed "{activity}" ({playstate})')
                previous_activity, previous_playstate = activity, session_paused

        elif previous_activity is not None:
            try:
                await discord_rpc.clear()
            except (PyPresenceException, ConnectionRefusedError) as e:
                logger.debug(e)
                await await_connection(discord_rpc, refresh_rate)
                await asyncio.sleep(refresh_rate)
                continue
            logger.info('Activity Cleared')
            previous_activity, previous_playstate = None, False

        await asyncio.sleep(refresh_rate)


def start_discord_rpc(
    ini_path: str, log_path: str | None = None, log_queue: Queue[LogRecord] | None = None
) -> None:
    config = load_config(ini_path)
    log_level = config.get('LOG_LEVEL', 'INFO').upper()
    refresh_rate = max(1, config.getint('REFRESH_RATE', 5))

    logger.setLevel(log_level)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    if log_path is not None:
        file_hdlr = logging.FileHandler(log_path, encoding='utf-8')
        file_hdlr.setFormatter(formatter)
        logger.addHandler(file_hdlr)
    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    logger.addHandler(stream_hdlr)
    if log_queue is not None:
        queue_hdlr = handlers.QueueHandler(log_queue)
        logger.addHandler(queue_hdlr)

    def handle_shutdown(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        asyncio.run(monitor_activity(config, refresh_rate))
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
