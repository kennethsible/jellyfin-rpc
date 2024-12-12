import argparse
import json
import logging
import logging.handlers
import sys
import time
import uuid
from configparser import ConfigParser, SectionProxy

import requests
import urllib3
from jellyfin_apiclient_python import JellyfinClient, api
from jellyfin_apiclient_python.exceptions import HTTPException
from pypresence import DiscordNotFound, PipeClosed, Presence
from requests.exceptions import RequestException
from urllib3.exceptions import InsecureRequestWarning

CLIENT_ID = '1238889120672120853'
DEFAULT_POSTER_URL = 'jellyfin_icon'

logger = logging.getLogger(__name__)
urllib3.disable_warnings(InsecureRequestWarning)


def get_config(ini_path: str) -> SectionProxy:
    config = ConfigParser()
    config.read(ini_path)
    return config['DEFAULT']


def get_user_id(config: SectionProxy) -> str:
    url = config['JELLYFIN_HOST'] + '/Users'
    headers = {'Accept': 'application/json', 'X-Emby-Token': config['API_TOKEN']}
    user_data = requests.request('GET', url, headers=headers, verify=False)
    for user in user_data.json():
        if config['USERNAME'] in user['Name']:
            return user['Id']
    raise ValueError(f'{config["USERNAME"]} Not Found.')


def get_jellyfin_api(config: SectionProxy, refresh_rate: int) -> api.API:
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
                            'AccessToken': config['API_TOKEN'],
                            'UserId': get_user_id(config),
                            'DateLastAccessed': 0,
                        }
                    ]
                },
                discover=False,
            )
            logger.debug('Connection Established: Jellyfin.')
        except (RequestException, json.JSONDecodeError):
            logger.error('Connection Failed: Jellyfin. Retrying...')
            time.sleep(refresh_rate)
            continue
        return client.jellyfin


def get_series_poster(api_key: str, imdb_id: str, season: int) -> str:
    response = requests.get(
        f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id"
    )
    tmdb_id = json.loads(response.text)['tv_episode_results'][0]['show_id']
    response = requests.get(
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/images?api_key={api_key}"
    )
    try:
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except KeyError:
        logger.warning('No Poster Available on TMDB. Skipping...')
        return DEFAULT_POSTER_URL


def get_movie_poster(api_key: str, imdb_id: str) -> str:
    response = requests.get(
        f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id"
    )
    tmdb_id = json.loads(response.text)['movie_results'][0]['id']
    response = requests.get(
        f"https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={api_key}"
    )
    try:
        return (
            'https://image.tmdb.org/t/p/w185/'
            + json.loads(response.text)['posters'][0]['file_path']
        )
    except KeyError:
        logger.warning('Connection Failed: TMDB. Skipping...')
        return DEFAULT_POSTER_URL


def await_connection(discord_rpc: Presence, refresh_rate: int):
    while True:
        try:
            discord_rpc.connect()
            logger.debug('Connection Established: Discord.')
        except DiscordNotFound:
            logger.error('Connection Failed: Discord. Retrying...')
            time.sleep(refresh_rate)
            continue
        break


def set_discord_rpc(config: SectionProxy, *, refresh_rate: int = 10):
    discord_rpc = Presence(CLIENT_ID)
    await_connection(discord_rpc, refresh_rate)
    jellyfin_api = get_jellyfin_api(config, refresh_rate)
    previous_details = ''
    while True:
        try:
            session = next(
                session
                for session in jellyfin_api.sessions()
                if config['USERNAME'] == session['UserName']
            )
        except StopIteration:
            session = None
        except HTTPException:
            jellyfin_api = get_jellyfin_api(config, refresh_rate)
            continue
        if session is not None and 'NowPlayingItem' in session:
            media_types = config['MEDIA_TYPES'].split(',')
            match media_type := session['NowPlayingItem']['Type']:
                case 'Episode':
                    if 'Shows' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    season = session['NowPlayingItem']['ParentIndexNumber']
                    episode = session['NowPlayingItem']['IndexNumber']
                    state = ''
                    if 'SeriesName' in session['NowPlayingItem']:
                        state += session['NowPlayingItem']['SeriesName']
                    details = f'{f"S{season}:E{episode}"} - {session["NowPlayingItem"]["Name"]}'
                case 'Movie':
                    if 'Movies' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    state = ''
                    if 'Genres' in session['NowPlayingItem']:
                        state += ', '.join(session['NowPlayingItem']['Genres'])
                    details = session['NowPlayingItem']['Name']
                case 'Audio':
                    if 'Music' not in media_types:
                        time.sleep(refresh_rate)
                        continue
                    state = ''
                    if 'Artists' in session['NowPlayingItem']:
                        state += ', '.join(session['NowPlayingItem']['Artists'])
                    if 'Album' in session['NowPlayingItem']:
                        state += ' - ' + session['NowPlayingItem']['Album']
                    details = session['NowPlayingItem']['Name']
                case _:
                    logger.warning(f'Unsupported Media Type: {media_type}. Ignoring...')
                    time.sleep(refresh_rate)
                    continue  # raise NotImplementedError()
            if details != previous_details:
                poster_url = DEFAULT_POSTER_URL
                if media_type in ('Episode', 'Movie') and len(config['TMDB_API_KEY']) > 0:
                    try:
                        imdb_id = next(
                            external_url['Url']
                            for external_url in session['NowPlayingItem']['ExternalUrls']
                            if external_url['Name'] == 'IMDb'
                        ).split('/')[-1]
                    except StopIteration:
                        logger.warning('No IMDb ID Found. Skipping...')
                    else:
                        try:
                            if session['NowPlayingItem']['Type'] == 'Episode':
                                poster_url = get_series_poster(
                                    config['TMDB_API_KEY'], imdb_id, season
                                )
                            elif session['NowPlayingItem']['Type'] == 'Movie':
                                poster_url = get_movie_poster(config['TMDB_API_KEY'], imdb_id)
                        except RequestException:
                            logger.warning('Connection Failed: TMDB. Skipping...')
                try:
                    # source_id = session['NowPlayingItem']['Id']
                    # server_id = session['NowPlayingItem']['ServerId']
                    # url_path = f'web/#/details?id={source_id}&serverId={server_id}'
                    discord_rpc.update(
                        state=state,
                        details=details,
                        start=time.time(),
                        large_image=poster_url,
                        # buttons=[
                        #     {'label': 'Play on Jellyfin', 'url': config['JELLYFIN_HOST'] + url_path}
                        # ],
                    )
                    logger.info(f'RPC Updated: {details}.')
                except PipeClosed:
                    await_connection(discord_rpc, refresh_rate)
                    continue
                previous_details = details
        elif previous_details:
            try:
                discord_rpc.clear()
            except PipeClosed:
                await_connection(discord_rpc, refresh_rate)
                continue
            logger.info(f'RPC Cleared: {previous_details}.')
            previous_details = ''
        time.sleep(refresh_rate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-path', default='jellyfin_rpc.ini')
    parser.add_argument('--log-path', default='jellyfin_rpc.log')
    parser.add_argument('--refresh-rate', type=int, default=10)
    args = parser.parse_args()

    config = get_config(args.ini_path)
    logger.setLevel(config['LOG_LEVEL'])
    file_hdlr = logging.FileHandler(args.log_path)
    file_hdlr.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(file_hdlr)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    set_discord_rpc(config, refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
