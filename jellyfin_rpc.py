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
from pypresence import DiscordNotFound, PipeClosed, Presence

CLIENT_ID = '1238889120672120853'
DEFAULT_POSTER_URL = 'jellyfin_icon'

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error('Unhandled Exception:', exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception


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
    raise ValueError(f'{config["USERNAME"]} not found.')


def get_jellyfin_api(config: SectionProxy) -> api.API:
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
        logger.debug('Connection Failed: TMDB. Skipping...')
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
        logger.debug('Connection Failed: TMDB. Skipping...')
        return DEFAULT_POSTER_URL


def await_connection(RPC: Presence, refresh_rate: int):
    connection_failed = False
    while True:
        try:
            RPC.connect()
        except DiscordNotFound:
            logger.debug('Connection Failed: Discord. Retrying...')
            connection_failed = True
            time.sleep(refresh_rate)
            continue
        break
    if connection_failed:
        logger.debug('Connection Reestablished: Discord.')
        connection_failed = False


def set_discord_rpc(config: SectionProxy, *, refresh_rate: int = 10):
    RPC = Presence(CLIENT_ID)
    await_connection(RPC, refresh_rate)
    previous_details, connection_failed = '', False
    while True:
        try:
            jellyfin_api = get_jellyfin_api(config)
        except (json.JSONDecodeError, requests.exceptions.RequestException):
            logger.debug('Connection Failed: Jellyfin. Retrying...')
            connection_failed = True
            time.sleep(refresh_rate)
            continue
        if connection_failed:
            logger.debug('Connection Reestablished: Jellyfin.')
            connection_failed = False
        try:
            session = next(
                session
                for session in jellyfin_api.sessions()
                if config['USERNAME'] == session['UserName']
            )
        except StopIteration:
            session = None
        if session is not None and 'NowPlayingItem' in session:
            match media_type := session['NowPlayingItem']['Type']:
                case 'Episode':
                    season = session['NowPlayingItem']['ParentIndexNumber']
                    episode = session['NowPlayingItem']['IndexNumber']
                    state = session['NowPlayingItem']['SeriesName']
                    details = f'{f"S{season}:E{episode}"} - {session["NowPlayingItem"]["Name"]}'
                case 'Movie':
                    state = ', '.join(session['NowPlayingItem']['Genres'])
                    details = session['NowPlayingItem']['Name']
                case 'Audio':
                    album = session['NowPlayingItem']['Album']
                    artist = session['NowPlayingItem']['Artists'][0]
                    state = f'{album} - {artist}'
                    details = session['NowPlayingItem']['Name']
                case _:
                    logger.info(f'Unsupported Media Type: {media_type}. Ignoring...')
                    time.sleep(refresh_rate)
                    continue  # raise NotImplementedError()
            if details != previous_details:
                if media_type in ('Episode', 'Movie') and len(config['TMDB_API_KEY']) > 0:
                    try:
                        imdb_id = next(
                            external_url['Url']
                            for external_url in session['NowPlayingItem']['ExternalUrls']
                            if external_url['Name'] == 'IMDb'
                        ).split('/')[-1]
                    except StopIteration:
                        logger.debug('No IMDb ID Found. Skipping...')
                        poster_url = DEFAULT_POSTER_URL
                    else:
                        if session['NowPlayingItem']['Type'] == 'Episode':
                            poster_url = get_series_poster(config['TMDB_API_KEY'], imdb_id, season)
                        elif session['NowPlayingItem']['Type'] == 'Movie':
                            poster_url = get_movie_poster(config['TMDB_API_KEY'], imdb_id)
                else:
                    poster_url = DEFAULT_POSTER_URL
                try:
                    # source_id = session['NowPlayingItem']['Id']
                    # server_id = session['NowPlayingItem']['ServerId']
                    # url_path = f'web/#/details?id={source_id}&serverId={server_id}'
                    RPC.update(
                        state=state,
                        details=details,
                        start=time.time(),
                        large_image=poster_url,
                        # buttons=[
                        #     {'label': 'Play on Jellyfin', 'url': config['JELLYFIN_HOST'] + url_path}
                        # ],
                    )
                    logger.debug(f'RPC Updated: {details}.')
                except PipeClosed:
                    await_connection(RPC, refresh_rate)
                    continue
                previous_details = details
        elif previous_details:
            try:
                RPC.clear()
            except PipeClosed:
                await_connection(RPC, refresh_rate)
                continue
            logger.debug(f'RPC Cleared: {previous_details}.')
            previous_details = ''
        time.sleep(refresh_rate)


def main(log_path: str | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-config', default='jellyfin_rpc.ini')
    parser.add_argument('--refresh-rate', type=int, default=10)
    args = parser.parse_args()

    config = get_config(args.ini_config)
    logger.setLevel(config['LOG_LEVEL'])
    if log_path is not None:
        file_hdlr = logging.FileHandler(log_path)
        file_hdlr.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        logger.addHandler(file_hdlr)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    set_discord_rpc(config, refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
