import argparse
import configparser
import time
import uuid

import requests
import urllib3
from jellyfin_apiclient_python import JellyfinClient, api
from pypresence import Presence

urllib3.disable_warnings()

CLIENT_ID = '1238889120672120853'


def get_config() -> configparser.SectionProxy:
    config = configparser.ConfigParser()
    config.read('jellyfin_rpc.ini')
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


def set_discord_rpc(config: configparser.SectionProxy, *, refresh_rate: int = 10):
    RPC = Presence(CLIENT_ID)
    RPC.connect()
    flag_1 = flag_2 = False
    while True:
        for session in get_jellyfin_api(config).sessions():
            if config['USERNAME'] == session['UserName']:
                if 'NowPlayingItem' in session:
                    if not flag_2:
                        season = session['NowPlayingItem']['ParentIndexNumber']
                        episode = session['NowPlayingItem']['IndexNumber']
                        RPC.update(
                            state=session['NowPlayingItem']['SeriesName'],
                            details=f'{f"S{season}:E{episode}"} - {session["NowPlayingItem"]["Name"]}',
                            large_image='large_image',
                        )
                        flag_2 = True
                    flag_1 = True
        if not flag_1:
            RPC.clear()
            flag_2 = False
        flag_1 = False
        time.sleep(refresh_rate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-rate', type=int, default=10)
    args = parser.parse_args()
    set_discord_rpc(get_config(), refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
