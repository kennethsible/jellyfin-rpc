import argparse
import time
import uuid
from configparser import ConfigParser, SectionProxy

import requests
import urllib3
from jellyfin_apiclient_python import JellyfinClient, api
from pypresence import Presence

urllib3.disable_warnings()

CLIENT_ID = '1238889120672120853'


def get_config() -> SectionProxy:
    config = ConfigParser()
    config.read('jellyfin_rpc.ini')
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


def set_discord_rpc(config: SectionProxy, *, refresh_rate: int = 10):
    RPC = Presence(CLIENT_ID)
    RPC.connect()
    rpc_active = False
    while True:
        for session in get_jellyfin_api(config).sessions():
            if config['USERNAME'] == session['UserName']:
                if not rpc_active and 'NowPlayingItem' in session:
                    season = session['NowPlayingItem']['ParentIndexNumber']
                    episode = session['NowPlayingItem']['IndexNumber']
                    RPC.update(
                        state=session['NowPlayingItem']['SeriesName'],
                        details=f'{f"S{season}:E{episode}"} - {session["NowPlayingItem"]["Name"]}',
                        large_image='large_image',
                    )
                    rpc_active = True
                elif rpc_active and 'NowPlayingItem' not in session:
                    RPC.clear()
                    rpc_active = False
        time.sleep(refresh_rate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-rate', type=int, default=10)
    args = parser.parse_args()
    set_discord_rpc(get_config(), refresh_rate=args.refresh_rate)


if __name__ == '__main__':
    main()
