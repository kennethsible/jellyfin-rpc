import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime
from configparser import ConfigParser, SectionProxy
from json.decoder import JSONDecodeError
from logging import handlers
from multiprocessing.queues import Queue

import requests
from jellyfin_apiclient_python import JellyfinClient, api
from jellyfin_apiclient_python.exceptions import HTTPException
from pypresence.exceptions import PyPresenceException
from pypresence.presence import Presence
from pypresence.types import ActivityType, StatusDisplayType
from requests.exceptions import RequestException

CLIENT_ID = '1238889120672120853'
logger = logging.getLogger('RPC')


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


def get_jf_api(config: SectionProxy, refresh_rate: int) -> tuple[api.API, str | None]:
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
            time.sleep(refresh_rate)
            continue
        except KeyError as e:
            logger.error(f'Missing Key in INI Config: {e}')
            sys.exit(0)
        return client.jellyfin, server_name


def ping_tmdb_api(api_key: str):
    try:
        response = requests.get(f'https://api.themoviedb.org/3/configuration?api_key={api_key}')
        response.raise_for_status()
        logger.info('Connected to TMDB API')
    except RequestException as e:
        logger.debug(e)
        logger.warning('TMDB API Connection Failed. Skipping...')


def get_series_poster(api_key: str, tmdb_id: str) -> str:
    try:
        response = requests.get(
            f'https://api.themoviedb.org/3/tv/{tmdb_id}/images?api_key={api_key}'
        )
        response.raise_for_status()
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except (RequestException, KeyError, JSONDecodeError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Poster Available on TMDB. Skipping...')
        return 'large_image'


def get_season_poster(api_key: str, tmdb_id: str, season: int | None = None) -> str:
    if season is None:
        return get_series_poster(api_key, tmdb_id)
    try:
        response = requests.get(
            f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images?api_key={api_key}'
        )
        response.raise_for_status()
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except (RequestException, KeyError, JSONDecodeError, IndexError):
        return get_series_poster(api_key, tmdb_id)


def get_movie_poster(api_key: str, tmdb_id: str) -> str:
    try:
        response = requests.get(
            f'https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={api_key}'
        )
        response.raise_for_status()
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except (RequestException, KeyError, JSONDecodeError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Poster Available on TMDB. Skipping...')
        return 'large_image'


def get_release_group_cover(group_id: str) -> str:
    try:
        response = requests.get(f'https://coverartarchive.org/release-group/{group_id}')
        response.raise_for_status()
        return json.loads(response.text)['images'][0]['image']
    except (RequestException, KeyError, JSONDecodeError, IndexError) as e:
        logger.debug(e)
        logger.warning('No Cover Art Available on MusicBrainz. Skipping...')
    return 'large_image'


def get_release_cover(group_id: str, release_id: str | None = None) -> str:
    if release_id is None:
        return get_release_group_cover(group_id)
    try:
        response = requests.get(f'https://coverartarchive.org/release/{release_id}')
        response.raise_for_status()
        return json.loads(response.text)['images'][0]['image']
    except (RequestException, KeyError, JSONDecodeError, IndexError):
        return get_release_group_cover(group_id)


def await_connection(discord_rpc: Presence, refresh_rate: int):
    initial_attempt = True
    while True:
        try:
            discord_rpc.connect()
            logger.info('Connected to Discord Client')
        except (PyPresenceException, ConnectionRefusedError) as e:
            if initial_attempt:
                logger.debug(e)
                logger.error('Discord Client Connection Failed. Retrying...')
            initial_attempt = False
            time.sleep(refresh_rate)
            continue
        break


def run_main_loop(config: SectionProxy, refresh_rate: int):
    client_id = config.get('DISCORD_CLIENT_ID', CLIENT_ID)
    discord_rpc = Presence(client_id)
    await_connection(discord_rpc, refresh_rate)
    jf_api, server_name = get_jf_api(config, refresh_rate)
    if config.get('TMDB_API_KEY'):
        ping_tmdb_api(config['TMDB_API_KEY'])
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_jf_icon = config.getboolean('SHOW_JELLYFIN_ICON', False)
    show_livetv_image = config.getboolean('SHOW_LIVETV_IMAGE', False)

    activity = previous_activity = None
    previous_warning = previous_playstate = False
    while True:
        try:
            session = next(
                session
                for session in jf_api.sessions()
                if config['JELLYFIN_USERNAME'] == session['UserName']
            )
        except StopIteration:
            session = None
        except (HTTPException, KeyError) as e:
            logger.debug(e)
            jf_api, server_name = get_jf_api(config, refresh_rate)
            continue

        if session and 'NowPlayingItem' in session:
            try:
                session_paused = session['PlayState']['IsPaused']
            except KeyError as e:
                logger.warning(f'Missing Key in Session Data: {e}')
                session_paused = False
            if session_paused and not show_when_paused:
                if previous_activity is not None:
                    try:
                        discord_rpc.clear()
                    except PyPresenceException as e:
                        logger.debug(e)
                        await_connection(discord_rpc, refresh_rate)
                        continue
                    logger.info('Activity Cleared')
                    previous_activity, previous_playstate = None, False
                time.sleep(refresh_rate)
                continue

            state = details = None
            try:
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
                        details = media_dict['SeriesName']
                        state = f'{f"S{season}:E{episode}"} - {media_dict["Name"]}'
                        activity = state
                    case 'Movie':
                        activity_type = ActivityType.WATCHING
                        if 'Movies' not in media_types:
                            time.sleep(refresh_rate)
                            continue
                        details = media_dict['Name']
                        activity = details
                    case 'Audio':
                        activity_type = ActivityType.LISTENING
                        if 'Music' not in media_types:
                            time.sleep(refresh_rate)
                            continue
                        if 'Artists' in media_dict:
                            state = ', '.join(media_dict['Artists'])
                            if 'Album' in media_dict:
                                state += f' - {media_dict["Album"]}'
                        elif 'Album' in media_dict:
                            state = media_dict['Album']
                        details = media_dict['Name']
                        activity = details
                    
                    case 'TvChannel':
                        activity_type = ActivityType.WATCHING

                        channel_name = media_dict.get('Name', 'Live TV')
                        prog = media_dict.get('CurrentProgram') or {}

                        details = prog.get('Name') or channel_name

                        episode_title = prog.get('EpisodeTitle')
                        if episode_title:
                            state = f"{channel_name} - {episode_title}"
                        else:
                            state = channel_name

                        activity = state

                    case _:
                        logger.warning(f'Unsupported Media Type "{media_type}". Ignoring...')
                        time.sleep(refresh_rate)
                        continue  # raise NotImplementedError()
            except KeyError as e:
                if not previous_warning:
                    logger.warning(f'Missing Key in Session Data: {e}. Skipping...')
                    previous_warning = True
                time.sleep(refresh_rate)
                continue
            previous_warning = False
            if len(details) < 2:  # e.g., Chinese characters
                details += ' '

            if previous_activity != activity or previous_playstate != session_paused:
                poster_url = 'large_image'
                state_url = large_url = details_url = None

                if media_type == 'Episode' and config.get('TMDB_API_KEY'):
                    try:
                        series = jf_api.get_item(media_dict['SeriesId'])
                        tmdb_id = series['ProviderIds']['Tmdb']
                    except KeyError:
                        logger.warning('No TMDB ID Found. Skipping...')
                    else:
                        season = None
                        if 'ParentIndexNumber' in media_dict:
                            season = media_dict['ParentIndexNumber']
                        poster_url = get_season_poster(config['TMDB_API_KEY'], tmdb_id, season)
                        details_url = f'https://www.themoviedb.org/tv/{tmdb_id}'
                        if 'IndexNumber' in media_dict:
                            episode = media_dict['IndexNumber']
                            state_url = f'{details_url}/season/{season}/episode/{episode}'
                        large_url = f'{details_url}/season/{season}'

                elif media_type == 'Movie' and config.get('TMDB_API_KEY'):
                    try:
                        tmdb_id = media_dict['ProviderIds']['Tmdb']
                    except KeyError:
                        logger.warning('No TMDB ID Found. Skipping...')
                    else:
                        poster_url = get_movie_poster(config['TMDB_API_KEY'], tmdb_id)
                        details_url = f'https://www.themoviedb.org/movie/{tmdb_id}'
                        large_url = details_url

                elif media_type == 'Audio':
                    try:
                        group_id = media_dict['ProviderIds']['MusicBrainzReleaseGroup']
                    except KeyError:
                        logger.warning('No MusicBrainz ID Found. Skipping...')
                    else:
                        try:
                            release_id = media_dict['ProviderIds']['MusicBrainzAlbum']
                        except KeyError:
                            release_id = None
                        poster_url = get_release_cover(group_id, release_id)
                        if 'MusicBrainzTrack' in media_dict['ProviderIds']:
                            track_id = media_dict['ProviderIds']['MusicBrainzTrack']
                            details_url = f'https://musicbrainz.org/track/{track_id}'
                        state_url = f'https://musicbrainz.org/release-group/{group_id}'
                        if release_id is None:
                            large_url = f'https://musicbrainz.org/release/{release_id}'
                        else:
                            large_url = state_url
                
                elif media_type == 'TvChannel':
                    if show_livetv_image:
                        try:
                            host = config.get('JELLYFIN_HOST', '').rstrip('/')
                            image_tag = None
                            item_id = None

                            prog = media_dict.get('CurrentProgram') or {}
                            prog_tags = prog.get('ImageTags') or {}
                            chan_tags = media_dict.get('ImageTags') or {}

                            if prog_tags.get('Primary') and prog.get('Id'):
                                image_tag = prog_tags['Primary']
                                item_id = prog['Id']
                            elif chan_tags.get('Primary') and media_dict.get('Id'):
                                image_tag = chan_tags['Primary']
                                item_id = media_dict['Id']

                            if host and image_tag and item_id:
                                poster_url = f"{host}/Items/{item_id}/Images/Primary?tag={image_tag}"
                                large_url = f"{host}/web/index.html#!/details?id={item_id}"

                        except Exception as e:
                            logger.debug(f"Failed to generate TvChannel image: {e}")

                if session_paused:
                    start_time, end_time = time.time(), None
                else:
                    try:
                        current_time = time.time()
                        position_ticks = session['PlayState']['PositionTicks']
                        start_time = current_time - position_ticks / 10_000_000
                        runtime_ticks = media_dict['RunTimeTicks']
                        end_time = start_time + runtime_ticks / 10_000_000
                    except KeyError:
                        start_time, end_time = time.time(), None
                small_image = 'small_image' if show_jf_icon else None
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
                        large_url=large_url,
                        small_image=small_image,
                    )
                except PyPresenceException as e:
                    logger.debug(e)
                    await_connection(discord_rpc, refresh_rate)
                    continue

                if previous_activity is None or previous_activity != activity:
                    logger.info(f'Activity Updated "{activity}"')
                else:
                    playstate = 'Paused' if session_paused else 'Resumed'
                    logger.debug(f'PlayState Changed "{activity}" ({playstate})')
                previous_activity, previous_playstate = activity, session_paused

        elif previous_activity is not None:
            try:
                discord_rpc.clear()
            except PyPresenceException as e:
                logger.debug(e)
                await_connection(discord_rpc, refresh_rate)
                continue
            logger.info('Activity Cleared')
            previous_activity, previous_playstate = None, False
        time.sleep(refresh_rate)


def start_discord_rpc(ini_path: str, log_path: str | None = None, log_queue: Queue | None = None):
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

    run_main_loop(config, refresh_rate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-path', type=str, required=True)
    parser.add_argument('--log-path', type=str)
    args = parser.parse_args()

    start_discord_rpc(args.ini_path, args.log_path)


if __name__ == '__main__':
    main()
