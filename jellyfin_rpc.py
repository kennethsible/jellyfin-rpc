import argparse
import json
import logging
import sys
import time
import uuid
from configparser import ConfigParser, SectionProxy
from json.decoder import JSONDecodeError
from logging import handlers
from multiprocessing.queues import Queue

import requests
from jellyfin_apiclient_python import JellyfinClient, api
from jellyfin_apiclient_python.exceptions import HTTPException
from pypresence import DiscordNotFound, PipeClosed
from pypresence.presence import Presence
from pypresence.types import ActivityType, StatusDisplayType
from requests.exceptions import RequestException

CLIENT_ID = '1238889120672120853'
DEFAULT_POSTER_URL = 'jellyfin_icon'

logger = logging.getLogger('RPC')


def get_config(ini_path: str) -> SectionProxy:
    config = ConfigParser()
    config.read(ini_path)
    if config.get('DEFAULT', 'API_TOKEN', fallback=None):
        jf_api_key = config.get('DEFAULT', 'API_TOKEN')
        config.set('DEFAULT', 'JELLYFIN_API_KEY', jf_api_key)
    if config.get('DEFAULT', 'USERNAME', fallback=None):
        jf_username = config.get('DEFAULT', 'USERNAME')
        config.set('DEFAULT', 'JELLYFIN_USERNAME', jf_username)
    return config['DEFAULT']


def get_user_id(config: SectionProxy) -> str:
    url = config['JELLYFIN_HOST'] + '/Users'
    headers = {'Accept': 'application/json', 'X-Emby-Token': config['JELLYFIN_API_KEY']}
    user_data = requests.get(url, headers=headers, verify=True)
    user_data.raise_for_status()
    for user in user_data.json():
        if config['JELLYFIN_USERNAME'] in user['Name']:
            return user['Id']
    raise ValueError(config['JELLYFIN_USERNAME'])


def get_jf_api(config: SectionProxy, refresh_rate: int) -> tuple[api.API, str | None]:
    initial_attempt = True
    while True:
        try:
            client = JellyfinClient()
            client.config.app('jellyfin-rpc', '0.1.0', 'Discord RPC', uuid.uuid4())
            client.config.data['auth.ssl'] = True
            client.authenticate(
                {
                    'Servers': [
                        {
                            'address': config['JELLYFIN_HOST'],
                            'AccessToken': config['JELLYFIN_API_KEY'],
                            'UserId': get_user_id(config),
                            'DateLastAccessed': 0,
                        }
                    ]
                },
                discover=False,
            )
            server_name = None
            if config.getboolean('SHOW_SERVER_NAME', False):
                server_name = client.jellyfin.get_system_info().get('ServerName')
                logger.info(f'Connected to {server_name}')
            else:
                logger.info('Connected to Jellyfin')
        except (RequestException, JSONDecodeError, ValueError):
            if initial_attempt:
                logger.error('Connection to Jellyfin Failed. Retrying...')
            initial_attempt = False
            time.sleep(refresh_rate)
            continue
        return client.jellyfin, server_name


def get_series_poster(api_key: str, tmdb_id: str, season: int) -> str:
    response = requests.get(
        f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images?api_key={api_key}'
    )
    response.raise_for_status()
    try:
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except (KeyError, JSONDecodeError):
        response = requests.get(
            f'https://api.themoviedb.org/3/tv/{tmdb_id}/images?api_key={api_key}'
        )
        try:
            return (
                'https://image.tmdb.org/t/p/w185/'
                + json.loads(response.text)['posters'][0]['file_path']
            )
        except (KeyError, JSONDecodeError):
            logger.warning('No Poster Available on TMDB. Skipping...')
            return DEFAULT_POSTER_URL


def get_movie_poster(api_key: str, tmdb_id: str) -> str:
    response = requests.get(
        f'https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={api_key}'
    )
    response.raise_for_status()
    try:
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except (KeyError, JSONDecodeError):
        logger.warning('No Poster Available on TMDB. Skipping...')
        return DEFAULT_POSTER_URL


def get_album_cover(album_id: str, group_id: str) -> str:
    response = requests.get(f'https://coverartarchive.org/release/{album_id}')
    response.raise_for_status()
    try:
        return json.loads(response.text)['images'][0]['image']
    except (KeyError, JSONDecodeError):
        response = requests.get(f'https://coverartarchive.org/release-group/{group_id}')
        try:
            return json.loads(response.text)['images'][0]['image']
        except (KeyError, JSONDecodeError):
            logger.warning('No Cover Art Available on MusicBrainz. Skipping...')
        return DEFAULT_POSTER_URL


def await_connection(discord_rpc: Presence, refresh_rate: int):
    initial_attempt = True
    while True:
        try:
            discord_rpc.connect()
            logger.info('Connected to Discord')
        except (DiscordNotFound, ConnectionRefusedError):
            if initial_attempt:
                logger.error('Connection to Discord Failed. Retrying...')
            initial_attempt = False
            time.sleep(refresh_rate)
            continue
        break


def set_discord_rpc(config: SectionProxy, refresh_rate: int):
    discord_rpc = Presence(CLIENT_ID)
    await_connection(discord_rpc, refresh_rate)
    jf_api, server_name = get_jf_api(config, refresh_rate)
    activity, previous_activity, previous_playstate = None, None, False
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_jf_icon = config.getboolean('SHOW_JELLYFIN_ICON', False)

    while True:
        try:
            session = next(
                session
                for session in jf_api.sessions()
                if config['JELLYFIN_USERNAME'] == session['UserName']
            )
        except StopIteration:
            session = None
        except (HTTPException, KeyError):
            jf_api, server_name = get_jf_api(config, refresh_rate)
            continue

        if session and 'NowPlayingItem' in session:
            session_paused = session['PlayState']['IsPaused']
            if session_paused and not show_when_paused:
                if previous_activity:
                    try:
                        discord_rpc.clear()
                    except PipeClosed:
                        await_connection(discord_rpc, refresh_rate)
                        continue
                    logger.info(f'RPC Cleared for "{activity}"')
                    previous_activity, previous_playstate = None, False
                time.sleep(refresh_rate)
                continue

            media_dict = session['NowPlayingItem']
            media_types = config['MEDIA_TYPES'].split(',')
            match media_type := media_dict['Type']:
                case 'Episode':
                    activity_type = ActivityType.WATCHING
                    if 'Shows' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    season = media_dict['ParentIndexNumber']
                    episode = media_dict['IndexNumber']
                    state = f'{f"S{season}:E{episode}"} - {media_dict["Name"]}'
                    details = media_dict['SeriesName']
                    large_text = None
                    activity = state
                case 'Movie':
                    activity_type = ActivityType.WATCHING
                    if 'Movies' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    details = media_dict['Name']
                    state = large_text = None
                    activity = details
                case 'Audio':
                    activity_type = ActivityType.LISTENING
                    if 'Music' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    state = None
                    if 'Artists' in media_dict:
                        state = ', '.join(media_dict['Artists'])
                    details = media_dict['Name']
                    large_text = None
                    if 'Album' in media_dict:
                        large_text = media_dict['Album']
                    activity = details
                case _:
                    logger.warning(f'Unsupported Media Type "{media_type}". Ignoring...')
                    time.sleep(refresh_rate)
                    continue  # raise NotImplementedError()
            if len(details) < 2:  # e.g., Chinese characters
                details += ' '

            if previous_activity != activity or previous_playstate != session_paused:
                poster_url = DEFAULT_POSTER_URL
                state_url = large_url = details_url = None

                if media_type == 'Episode' and config.get('TMDB_API_KEY'):
                    try:
                        series = jf_api.get_item(media_dict['SeriesId'])
                        tmdb_id = series['ProviderIds']['Tmdb']
                    except KeyError:
                        logger.warning('No TMDB ID Found. Skipping...')
                    else:
                        season = media_dict['ParentIndexNumber']
                        try:
                            poster_url = get_series_poster(config['TMDB_API_KEY'], tmdb_id, season)
                        except RequestException:
                            logger.warning('Connection to TMDB Failed. Skipping...')
                        details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                        season = media_dict['ParentIndexNumber']
                        large_url = f'{details_url}/season/{season}'
                        episode = media_dict['IndexNumber']
                        state_url = f'{details_url}/season/{season}/episode/{episode}'

                elif media_type == 'Movie' and config.get('TMDB_API_KEY'):
                    try:
                        tmdb_id = media_dict['ProviderIds']['Tmdb']
                    except KeyError:
                        logger.warning('No TMDB ID Found. Skipping...')
                    else:
                        try:
                            poster_url = get_movie_poster(config['TMDB_API_KEY'], tmdb_id)
                        except RequestException:
                            logger.warning('Connection to TMDB Failed. Skipping...')
                        details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'
                        large_url = details_url

                elif media_type == 'Audio':
                    try:
                        group_id = media_dict['ProviderIds']['MusicBrainzReleaseGroup']
                        album_id = media_dict['ProviderIds']['MusicBrainzAlbum']
                    except KeyError:
                        logger.warning('No MusicBrainz ID Found. Skipping...')
                    else:
                        try:
                            poster_url = get_album_cover(album_id, group_id)
                        except RequestException:
                            logger.warning('Connection to MusicBrainz Failed. Skipping...')
                        if 'MusicBrainzTrack' in media_dict['ProviderIds']:
                            track_id = media_dict['ProviderIds']['MusicBrainzTrack']
                            details_url = f'https://musicbrainz.org/track/{track_id}'
                        large_url = f'https://musicbrainz.org/release/{album_id}'
                        if 'MusicBrainzAlbumArtist' in media_dict['ProviderIds']:
                            artist_id = media_dict['ProviderIds']['MusicBrainzAlbumArtist']
                            state_url = f'https://musicbrainz.org/artist/{artist_id}'

                if session_paused:
                    start_time, end_time = time.time(), None
                else:
                    current_time = time.time()
                    position_ticks = session['PlayState']['PositionTicks']
                    start_time = current_time - position_ticks / 10_000_000
                    runtime_ticks = media_dict['RunTimeTicks']
                    end_time = start_time + runtime_ticks / 10_000_000
                small_image = DEFAULT_POSTER_URL if show_jf_icon else None
                try:
                    discord_rpc.update(
                        activity_type=activity_type,
                        status_display_type=StatusDisplayType.DETAILS,
                        state=state,
                        state_url=state_url,
                        details=details,
                        details_url=details_url,
                        name=server_name,
                        start=start_time,
                        end=end_time,
                        large_image=poster_url,
                        large_text=large_text,
                        large_url=large_url,
                        small_image=small_image,
                    )
                except PipeClosed:
                    await_connection(discord_rpc, refresh_rate)
                    continue

                if not previous_activity:
                    logger.info(f'RPC Set for "{activity}"')
                elif previous_activity != activity:
                    logger.info(f'RPC Updated for "{activity}"')
                else:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    logger.info(f'PlayState Updated for "{activity}" ({playstate})')
                previous_activity, previous_playstate = activity, session_paused

        elif previous_activity:
            try:
                discord_rpc.clear()
            except PipeClosed:
                await_connection(discord_rpc, refresh_rate)
                continue
            logger.info(f'RPC Cleared for "{activity}"')
            previous_activity, previous_playstate = None, False
        time.sleep(refresh_rate)


def main(log_queue: Queue | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-path', default='jellyfin_rpc.ini')
    parser.add_argument('--log-path', default='jellyfin_rpc.log')
    parser.add_argument('--refresh-rate', type=int, default=5)
    args = parser.parse_args()

    config = get_config(args.ini_path)
    logger.setLevel(config.get('LOG_LEVEL', 'INFO'))
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    file_hdlr = logging.FileHandler(args.log_path, encoding='utf-8')
    file_hdlr.setFormatter(formatter)
    logger.addHandler(file_hdlr)
    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    logger.addHandler(stream_hdlr)
    if log_queue is not None:
        queue_hdlr = handlers.QueueHandler(log_queue)
        logger.addHandler(queue_hdlr)

    set_discord_rpc(config, refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
