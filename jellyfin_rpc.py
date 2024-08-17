import argparse
import configparser
import json
import time
import uuid

import requests
import urllib3
from jellyfin_apiclient_python import JellyfinClient, api
from pypresence import DiscordNotFound, PipeClosed, Presence

urllib3.disable_warnings()

CLIENT_ID = '1238889120672120853'


def get_config(ini_path: str) -> configparser.SectionProxy:
    config = configparser.ConfigParser()
    config.read(ini_path)
    return config['DEFAULT']


def get_user_id(config: configparser.SectionProxy) -> str:
    url = config['JELLYFIN_HOST'] + '/Users'
    headers = {'Accept': 'application/json', 'X-Emby-Token': config['API_TOKEN']}
    user_data = requests.request('GET', url, headers=headers, verify=False)
    for user in user_data.json():
        if config['USERNAME'] in user['Name']:
            return user['Id']
    raise ValueError(f'{config["USERNAME"]} not found.')


def get_jellyfin_api(config: configparser.SectionProxy) -> api.API:
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
    return 'https://image.tmdb.org/t/p/w185/' + json.loads(response.text)['posters'][0]['file_path']


def get_movie_poster(api_key: str, imdb_id: str) -> str:
    response = requests.get(
        f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id"
    )
    tmdb_id = json.loads(response.text)['movie_results'][0]['id']
    response = requests.get(
        f"https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={api_key}"
    )
    return 'https://image.tmdb.org/t/p/w185/' + json.loads(response.text)['posters'][0]['file_path']


def await_connection(RPC: Presence, refresh_rate: int):
    while True:
        try:
            RPC.connect()
        except DiscordNotFound:
            time.sleep(refresh_rate)
            continue
        break


def set_discord_rpc(ini_path: str, *, refresh_rate: int = 10):
    RPC = Presence(CLIENT_ID)
    await_connection(RPC, refresh_rate)
    config = get_config(ini_path)
    previous_details = ''
    while True:
        try:
            jellyfin_api = get_jellyfin_api(config)
        except json.JSONDecodeError:
            time.sleep(refresh_rate)
            continue
        try:
            session = next(
                session
                for session in jellyfin_api.sessions()
                if config['USERNAME'] == session['UserName']
            )
        except StopIteration:
            time.sleep(refresh_rate)
            continue
        if 'NowPlayingItem' in session:
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
                    continue  # raise NotImplementedError()
            if details != previous_details:
                if media_type in ('Episode', 'Movie') and len(config['tmdb_api_key']) > 0:
                    try:
                        imdb_id = next(
                            external_url['Url']
                            for external_url in session['NowPlayingItem']['ExternalUrls']
                            if external_url['Name'] == 'IMDb'
                        ).split('/')[-1]
                    except StopIteration:
                        poster_url = 'jellyfin_icon'
                    else:
                        if session['NowPlayingItem']['Type'] == 'Episode':
                            poster_url = get_series_poster(config['tmdb_api_key'], imdb_id, season)
                        elif session['NowPlayingItem']['Type'] == 'Movie':
                            poster_url = get_movie_poster(config['tmdb_api_key'], imdb_id)
                else:
                    poster_url = 'jellyfin_icon'
                try:
                    RPC.update(
                        state=state,
                        details=details,
                        start=time.time(),
                        large_image=poster_url,
                    )
                except PipeClosed:
                    await_connection(RPC, refresh_rate)
                    continue
                previous_details = details
        else:
            previous_details = ''
            try:
                RPC.clear()
            except PipeClosed:
                await_connection(RPC, refresh_rate)
                continue
        time.sleep(refresh_rate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ini-config', default='jellyfin_rpc.ini')
    parser.add_argument('--refresh-rate', type=int, default=10)
    args = parser.parse_args()

    set_discord_rpc(args.ini_config, refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
