import functools
import logging
import multiprocessing as mp
import os
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
from configparser import ConfigParser, SectionProxy
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from typing import Any, Callable, TypedDict, cast

import certifi
import customtkinter as ctk
import pystray
import requests
from PIL import Image
from requests.exceptions import RequestException

from jellyfin_rpc import __version__, start_discord_rpc
from jellyfin_rpc.main import build_auth_header, get_device_id, load_config, parse_delimited_list

button_connect_text = ''
logger = logging.getLogger('GUI')


class RPCProcess:
    def __init__(self, target: Callable[[Queue[LogRecord]], None], log_queue: Queue[LogRecord]):
        self.target = target
        self.log_queue = log_queue
        self.process: mp.Process | None = None

    def start(self) -> None:
        self.process = mp.Process(target=self.target, args=(self.log_queue,))
        self.process.start()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.is_alive():
            self.process.terminate()
            self.process.join()
            logger.info('RPC Stopped')

    def has_failed(self) -> bool:
        if self.process is None:
            return False
        if self.process.exitcode is None:
            return False
        if self.process.exitcode in (0, -15):
            self.process = None
            return False
        logger.error('RPC Crashed')
        self.process = None
        return True


class RPCLogger:
    def __init__(
        self, frame: ctk.CTkFrame, log_queue: Queue[LogRecord], text_widget: ctk.CTkTextbox
    ):
        self.frame = frame
        self.log_queue = log_queue
        self.text_widget = text_widget

        self.text_widget.tag_config('DEBUG', foreground='#95A5A6')
        self.text_widget.tag_config('INFO', foreground='#3DAEE9')
        self.text_widget.tag_config('WARNING', foreground='#F67400')
        self.text_widget.tag_config('ERROR', foreground='#DA4453')
        self.text_widget.tag_config('CRITICAL', foreground='#DA4453')

        self.frame.after(100, self.poll_log_queue)

    def poll_log_queue(self) -> None:
        while not self.log_queue.empty():
            record = self.log_queue.get_nowait()
            self.display_record(record)
        self.frame.after(100, self.poll_log_queue)

    def display_record(self, record: LogRecord) -> None:
        message = self.format_log_record(record)
        self.text_widget.configure(state='normal')
        start_index = self.text_widget.index('end-1c')
        self.text_widget.insert(ctk.END, message)

        end_index = f'{start_index}+{len(record.levelname)}c'
        self.text_widget.tag_add(record.levelname, start_index, end_index)

        if message.rstrip().endswith('Open Setup Guide'):
            tk_text = self.text_widget._textbox
            tk_text.tag_add('link', '1.6', '1.22')
            mode_index = 0 if ctk.get_appearance_mode() == 'Light' else 1
            link_color = ctk.ThemeManager.theme['CTkButton']['fg_color'][mode_index]
            bold_font = ctk.CTkFont(family=tk_text.cget('font'))
            bold_font.configure(weight='bold')
            tk_text.tag_configure('link', foreground=link_color, font=bold_font)
            tk_text.tag_bind(
                'link',
                '<Button-1>',
                lambda _: webbrowser.open_new_tab(
                    'https://github.com/kennethsible/jellyfin-rpc?tab=readme-ov-file#configuration'
                ),
            )
            tk_text.tag_bind('link', '<Enter>', lambda event: event.widget.config(cursor='hand2'))
            tk_text.tag_bind('link', '<Leave>', lambda event: event.widget.config(cursor=''))

        self.text_widget.configure(state='disabled')
        self.text_widget.see(ctk.END)

    def format_log_record(self, record: LogRecord) -> str:
        return f'{record.levelname}: {record.getMessage()}\n'


class LibrarySelectorWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        config: SectionProxy,
        jf_host: str,
        jf_api_key: str,
        jf_username: str,
        var_filter_mode: ctk.StringVar,
        var_filter_libraries: ctk.StringVar,
    ):
        super().__init__(master)
        self.title('Jellyfin RPC')
        self.geometry('220x350')
        self.transient(master)
        self.grab_set()

        self.jf_host = jf_host
        self.jf_api_key = jf_api_key
        self.jf_username = jf_username
        self.var_filter_libraries = var_filter_libraries
        self.checkbox_map: dict[str, ctk.BooleanVar] = {}

        self.scroll_frame = ctk.CTkScrollableFrame(
            master=self, label_text=f'{var_filter_mode.get()}ed Libraries'
        )
        self.scroll_frame.pack(fill='both', expand=True, padx=10, pady=10)

        self.button_save_selection = ctk.CTkButton(
            master=self, text='Save Selection', command=self.save_selection
        )
        self.button_save_selection.pack(pady=(5, 10))

        self.retrieve_libraries(config)

    def retrieve_libraries(self, config: SectionProxy):
        device_id = get_device_id(config)
        headers = {
            'Accept': 'application/json',
            'Authorization': build_auth_header(device_id, self.jf_api_key),
        }
        try:
            response = requests.get(
                f'{self.jf_host}/Users', headers=headers, timeout=5, verify=certifi.where()
            )
            response.raise_for_status()
            users_data = response.json()

            user_id = None
            for user in users_data:
                if self.jf_username == user.get('Name', ''):
                    user_id = user.get('Id')
            if user_id is None:
                ctk.CTkLabel(self.scroll_frame, text=f'User Not Found: {self.jf_username}').pack()
                return

            response = requests.get(
                f'{self.jf_host}/Users/{user_id}/Views',
                headers=headers,
                timeout=5,
                verify=certifi.where(),
            )
            response.raise_for_status()
            views_data = response.json()
            libraries = views_data.get('Items', [])
            if not libraries:
                ctk.CTkLabel(self.scroll_frame, text='No Libraries Retrieved.').pack()
                return

            filter_libraries = [
                x.strip() for x in self.var_filter_libraries.get().split(',') if x.strip()
            ]
            for library in libraries:
                library_id = library.get('Id')
                library_name = library.get('Name')

                var_checkbox = ctk.BooleanVar(value=library_id in filter_libraries)
                checkbox = ctk.CTkCheckBox(
                    master=self.scroll_frame, text=library_name, variable=var_checkbox
                )
                checkbox.pack(anchor='w', pady=5, padx=10)

                self.checkbox_map[library_id] = var_checkbox

        except RequestException as e:
            logger.error(f'Failed to Retrieve Libraries: {e}')
            ctk.CTkLabel(self.scroll_frame, text='Error Retrieving Libraries.').pack()

    def save_selection(self):
        library_ids = [library_id for library_id, var in self.checkbox_map.items() if var.get()]
        self.var_filter_libraries.set(','.join(library_ids))
        self.destroy()


def save_config(
    ini_path: str,
    entries: dict[str, dict[str, Any]],
    checkboxes: dict[str, ctk.CTkCheckBox],
    var_filter_mode: ctk.StringVar,
    var_filter_libraries: ctk.StringVar,
    var_log_level: ctk.StringVar,
    var_polling_rate: ctk.StringVar,
    var_seek_threshold: ctk.StringVar,
) -> None:
    config = ConfigParser()
    config.read(ini_path)
    for key in (
        'JELLYFIN_HOST',
        'JELLYFIN_API_KEY',
        'JELLYFIN_USERNAME',
        'TMDB_API_KEY',
        'POSTER_LANGUAGES',
    ):
        config.set('DEFAULT', key, entries[key]['entry'].get())
    config.set('DEFAULT', 'FILTER_MODE', var_filter_mode.get())
    config.set('DEFAULT', 'FILTER_LIBRARIES', var_filter_libraries.get())
    config.set('DEFAULT', 'POLLING_RATE', var_polling_rate.get().rstrip('s'))
    config.set('DEFAULT', 'SEEK_THRESHOLD', var_seek_threshold.get().rstrip('s'))
    config.set('DEFAULT', 'LOG_LEVEL', var_log_level.get())

    media_types = []
    if checkboxes['MOVIES']._variable.get():
        media_types.append('Movies')
    if checkboxes['SHOWS']._variable.get():
        media_types.append('Shows')
    if checkboxes['MUSIC']._variable.get():
        media_types.append('Music')
    config.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))

    for key in (
        'SHOW_WHEN_PAUSED',
        'SHOW_SERVER_NAME',
        'SHOW_JELLYFIN_LOGO',
        'ALWAYS_USE_TMDB',
        'TEXTLESS_POSTERS',
        'SEASON_OVER_SERIES',
        'ALWAYS_USE_MUSICBRAINZ',
        'RELEASE_OVER_GROUP',
        'START_MINIMIZED',
        'MINIMIZE_ON_CLOSE',
    ):
        config.set('DEFAULT', key, str(bool(checkboxes[key]._variable.get())).lower())

    with open(ini_path, 'w') as ini_file:
        config.write(ini_file)


def on_click(
    button_connect: ctk.CTkButton,
    entries: dict[str, dict[str, Any]],
    rpc_process: RPCProcess,
    tray_icon: pystray._base.Icon | None = None,
    only_disconnect: bool = False,
) -> None:
    global button_connect_text
    if tray_icon is not None:
        tray_icon.title = f'Jellyfin RPC\n{button_connect_text}ed'
    if button_connect_text == 'Connect' and not only_disconnect:
        rpc_process.start()
        for entry in entries.values():
            show = '*' if entry['obfuscate'] else ''
            entry['entry'].configure(state='readonly', show=show)
            entry['entry'].update()
        button_connect_text = 'Disconnect'
        button_connect.configure(text=button_connect_text)
    else:
        rpc_process.stop()
        for entry in entries.values():
            entry['entry'].configure(state='normal', show='')
            entry['entry'].update()
        button_connect_text = 'Connect'
        button_connect.configure(text=button_connect_text)
    if tray_icon is not None:
        tray_icon.update_menu()
    button_connect.update()


def on_maximize(
    label_update: ctk.CTkLabel, frame_grid: ctk.CTkFrame, frame_bottom: ctk.CTkFrame, root: ctk.CTk
) -> None:
    threading.Thread(
        target=check_for_updates, args=(label_update, frame_grid, frame_bottom, root), daemon=True
    ).start()
    root.after(0, root.deiconify)


def on_close(
    root: ctk.CTk, rpc_process: RPCProcess, tray_icon: pystray._base.Icon | None = None
) -> None:
    rpc_process.stop()
    if tray_icon is not None:
        tray_icon.visible = False
        tray_icon.stop()
    root.destroy()


def set_close_behavior(
    root: ctk.CTk, on_close_callback: Callable[[], None], withdraw: bool
) -> None:
    if withdraw:
        if sys.platform == 'linux':
            root.protocol('WM_DELETE_WINDOW', root.iconify)
        else:
            root.protocol('WM_DELETE_WINDOW', root.withdraw)
    else:
        root.protocol('WM_DELETE_WINDOW', on_close_callback)


def get_executable_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    else:
        return os.path.abspath(__file__)


if sys.platform == 'win32':
    import winreg

    def set_startup_status(enabled: bool) -> None:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                exe_path = get_executable_path()
                winreg.SetValueEx(key, 'Jellyfin RPC', 0, winreg.REG_SZ, f'"{exe_path}"')
            else:
                try:
                    winreg.DeleteValue(key, 'Jellyfin RPC')
                except FileNotFoundError:
                    pass

    def get_startup_status() -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run',
                0,
                winreg.KEY_READ,
            ) as key:
                value, _ = winreg.QueryValueEx(key, 'Jellyfin RPC')
            exe_path, stored_path = get_executable_path(), value.strip('"')
            if os.path.normcase(exe_path) != os.path.normcase(stored_path):
                set_startup_status(True)
            return True
        except FileNotFoundError:
            return False


def open_file(filepath: str) -> None:
    if sys.platform == 'win32':
        os.startfile(filepath)
    elif sys.platform == 'darwin':
        subprocess.call(('open', filepath))
    else:
        subprocess.call(('xdg-open', filepath))


def parse_version(version_tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version_tag.lstrip('v').split('.'))


def check_for_updates(
    label_update: ctk.CTkLabel, frame_grid: ctk.CTkFrame, frame_bottom: ctk.CTkFrame, root: ctk.CTk
) -> None:
    try:
        response = requests.get(
            'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest',
            timeout=5,
            verify=certifi.where(),
        )
        response.raise_for_status()
        version_tag = response.json()['tag_name'].lstrip('v')
        version_tuple = parse_version(version_tag)

        if parse_version(__version__) < version_tuple:
            label_text = f'Update Available ({__version__} \u2192 {version_tag})'
            label_font = ctk.CTkFont(family='Roboto', size=14, weight='bold')

            def show_label_update() -> None:
                label_update.configure(text=label_text, font=label_font)
                frame_grid.grid(row=1, column=0, sticky='nsew', padx=10, pady=5)
                frame_bottom.grid(row=2, column=0, sticky='ew', padx=10, pady=10)
                root.children['!ctkframe'].grid_rowconfigure(0, weight=0)
                root.children['!ctkframe'].grid_rowconfigure(1, weight=1)
                root.children['!ctkframe'].grid_rowconfigure(2, weight=0)
                label_update.grid(row=0, column=0, pady=(10, 5), padx=10, sticky='ew')

                root.update_idletasks()
                root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

            label_update.after(0, show_label_update)

    except (RequestException, JSONDecodeError, KeyError) as e:
        logger.warning(f'GitHub Version Check Failed ({type(e).__name__}). Skipping...')
        logger.debug(e)


def setup_logging(log_level: int | str, log_path: str | None = None) -> Queue[LogRecord]:
    logger.setLevel(log_level)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    if log_path:
        file_hdlr = logging.FileHandler(log_path, encoding='utf-8')
        file_hdlr.setFormatter(formatter)
        logger.addHandler(file_hdlr)

    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    logger.addHandler(stream_hdlr)

    log_queue: Queue[LogRecord] = mp.Queue()
    queue_hdlr = handlers.QueueHandler(log_queue)
    logger.addHandler(queue_hdlr)
    return log_queue


def main() -> None:
    ini_name, log_name = 'jellyfin_rpc.ini', 'jellyfin_rpc.log'
    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_bundle_path = os.path.abspath(os.path.join(bundle_dir, ini_name))
    png_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    os.chdir(os.path.dirname(get_executable_path()))

    data_dir = ''
    if sys.platform == 'win32':
        root_dir = os.getenv('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
        data_dir = os.path.join(root_dir, 'Jellyfin RPC')
    elif sys.platform == 'darwin':
        root_dir = os.path.expanduser('~/Library/Application Support')
        data_dir = os.path.join(root_dir, 'Jellyfin RPC')
    else:
        root_dir = os.getenv('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        data_dir = os.path.join(root_dir, 'jellyfin-rpc')
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        ini_path = os.path.join(data_dir, ini_name)
        log_path = os.path.join(data_dir, log_name)
    else:
        ini_path, log_path = ini_name, log_name
    if not os.path.isfile(ini_path):
        if data_dir and os.path.isfile(ini_name):
            logger.info(f'Migrating INI to {ini_path}')
            shutil.copyfile(ini_name, ini_path)
        else:
            logger.info(f'Extracting INI to {ini_path}')
            shutil.copyfile(ini_bundle_path, ini_path)

    config = load_config(ini_path)

    jf_host = config.get('JELLYFIN_HOST', '')
    jf_api_key = config.get('JELLYFIN_API_KEY', '')
    jf_username = config.get('JELLYFIN_USERNAME', '')

    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_server_name = config.getboolean('SHOW_SERVER_NAME', False)
    show_jf_logo = config.getboolean('SHOW_JELLYFIN_LOGO') or config.getboolean(
        'SHOW_JELLYFIN_ICON', False
    )

    tmdb_api_key = config.get('TMDB_API_KEY', '')
    poster_languages = config.get('POSTER_LANGUAGES', '')
    always_use_tmdb = config.getboolean('ALWAYS_USE_TMDB', False)
    textless_posters = config.getboolean('TEXTLESS_POSTERS', False)
    season_over_series = config.getboolean('SEASON_OVER_SERIES', False)

    always_use_musicbrainz = config.getboolean('ALWAYS_USE_MUSICBRAINZ', False)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', False)

    filter_mode = config.get('FILTER_MODE', 'BLACKLIST').capitalize()
    filter_libraries = config.get('FILTER_LIBRARIES', '')

    start_minimized = config.getboolean('START_MINIMIZED', True)
    minimize_on_close = config.getboolean('MINIMIZE_ON_CLOSE', True)

    polling_rate = max(1, config.getint('POLLING_RATE') or config.getint('REFRESH_RATE', 5))
    seek_threshold = max(1, config.getint('SEEK_THRESHOLD', 10))
    log_level = config.get('LOG_LEVEL', 'INFO').upper()
    log_queue = setup_logging(log_level, log_path)

    color_theme = 'dark' if sys.platform == 'linux' else 'system'
    appearance_mode = config.get('APPEARANCE_MODE', color_theme)
    ctk.set_appearance_mode(appearance_mode)

    root = ctk.CTk(className='jellyfin-rpc')
    root.title('Jellyfin RPC')
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    frame_main = ctk.CTkFrame(master=root)
    frame_main.grid(row=0, column=0, sticky='nsew')
    frame_main.grid_rowconfigure(0, weight=1)
    frame_main.grid_rowconfigure(1, weight=0)
    frame_main.grid_columnconfigure(0, weight=1)

    frame_grid = ctk.CTkFrame(master=frame_main, fg_color='transparent')
    frame_grid.grid(row=0, column=0, sticky='nsew', padx=10, pady=5)
    frame_grid.grid_rowconfigure(0, weight=1)
    frame_grid.grid_columnconfigure((0, 1, 2), weight=1, uniform='column')

    frame_bottom = ctk.CTkFrame(master=frame_main, fg_color='transparent')
    frame_bottom.grid(row=1, column=0, sticky='ew', padx=10, pady=10)

    col1 = ctk.CTkFrame(master=frame_grid, fg_color='transparent')
    col1.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
    col2 = ctk.CTkFrame(master=frame_grid, fg_color='transparent')
    col2.grid(row=0, column=1, sticky='nsew', padx=5)
    col3 = ctk.CTkFrame(master=frame_grid, fg_color='transparent')
    col3.grid(row=0, column=2, sticky='nsew', padx=(5, 0))

    label_update = ctk.CTkLabel(master=frame_main, cursor='hand2')
    label_update.bind(
        '<Button-1>',
        lambda _: webbrowser.open_new_tab('https://github.com/kennethsible/jellyfin-rpc/releases'),
    )

    threading.Thread(
        target=check_for_updates, args=(label_update, frame_grid, frame_bottom, root), daemon=True
    ).start()

    font_header = ctk.CTkFont(family='Roboto', size=14, weight='bold')
    font_label = ctk.CTkFont(size=12)

    label_jellyfin_settings = ctk.CTkLabel(master=col1, text='Jellyfin Settings', font=font_header)
    label_jellyfin_settings.pack(pady=(10, 0), padx=10)

    label_host = ctk.CTkLabel(master=col1, text='Jellyfin Host', font=font_label)
    label_host.pack(anchor='w', padx=10)

    var_jf_host = ctk.StringVar(value=jf_host)
    entry_jf_host = ctk.CTkEntry(master=col1, textvariable=var_jf_host)
    entry_jf_host.pack(pady=(0, 5), padx=10, fill='x')

    label_jf_api_key = ctk.CTkLabel(master=col1, text='Jellyfin API Key', font=font_label)
    label_jf_api_key.pack(anchor='w', padx=10)

    var_jf_api_key = ctk.StringVar(value=jf_api_key)
    entry_jf_api_key = ctk.CTkEntry(
        master=col1, textvariable=var_jf_api_key, placeholder_text='Leave Blank for Quick Connect'
    )
    entry_jf_api_key.pack(pady=(0, 5), padx=10, fill='x')

    label_jf_username = ctk.CTkLabel(master=col1, text='Jellyfin Username', font=font_label)
    label_jf_username.pack(anchor='w', padx=10)

    var_jf_username = ctk.StringVar(value=jf_username)
    entry_jf_username = ctk.CTkEntry(
        master=col1, textvariable=var_jf_username, placeholder_text='Leave Blank for Quick Connect'
    )
    entry_jf_username.pack(pady=(0, 5), padx=10, fill='x')

    def change_filter_mode(value: str) -> None:
        var_filter_mode.set(value)
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    var_filter_mode = ctk.StringVar(value=filter_mode.capitalize())
    segmented_filter_mode = ctk.CTkSegmentedButton(
        master=col1,
        values=['Blacklist', 'Whitelist'],
        variable=var_filter_mode,
        command=lambda value: change_filter_mode(value),
    )
    segmented_filter_mode.pack(pady=(10, 5), padx=10, fill='x')
    var_filter_libraries = ctk.StringVar(value=filter_libraries)

    def select_libraries() -> None:
        jf_host_str = entry_jf_host.get().rstrip('/')
        if not jf_host_str:
            logger.error('Missing Jellyfin Host')
            return
        jf_api_key_str = entry_jf_api_key.get()
        if not jf_api_key_str:
            logger.error('Missing Jellyfin API Key')
            return
        jf_username_str = entry_jf_username.get()

        LibrarySelectorWindow(
            root,
            config,
            jf_host_str,
            jf_api_key_str,
            jf_username_str,
            var_filter_mode,
            var_filter_libraries,
        )
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    button_select_libraries = ctk.CTkButton(
        master=col1, text='Select Libraries', command=select_libraries
    )
    button_select_libraries.pack(pady=5, padx=10, fill='x')

    textbox_status_monitor = ctk.CTkTextbox(master=col1, height=100)
    textbox_status_monitor.configure(state='disabled')
    textbox_status_monitor.pack(pady=(10, 0), padx=10, fill='both', expand=True)

    label_poster_settings = ctk.CTkLabel(master=col2, text='Poster Settings', font=font_header)
    label_poster_settings.pack(pady=(10, 0), padx=10)

    label_tmdb_api_key = ctk.CTkLabel(master=col2, text='TMDB API Key', font=font_label)
    label_tmdb_api_key.pack(anchor='w', padx=10)

    var_tmdb_api_key = ctk.StringVar(value=tmdb_api_key or None)
    entry_tmdb_api_key = ctk.CTkEntry(
        master=col2, textvariable=var_tmdb_api_key, placeholder_text='Leave Blank to Disable'
    )
    entry_tmdb_api_key.pack(pady=(0, 5), padx=10, fill='x')

    label_languages = ctk.CTkLabel(master=col2, text='Poster Language(s)')
    label_languages.pack(anchor='w', padx=10)

    var_languages = ctk.StringVar(value=poster_languages)
    entry_languages = ctk.CTkEntry(
        master=col2, textvariable=var_languages, placeholder_text='Leave Blank to Disable'
    )
    entry_languages.pack(pady=(0, 5), padx=10, fill='x')

    var_always_use_tmdb = ctk.IntVar(value=always_use_tmdb)
    checkbox_always_use_tmdb = ctk.CTkCheckBox(
        master=col2, text='Always Use The Movie Database', variable=var_always_use_tmdb
    )
    checkbox_always_use_tmdb.pack(anchor='w', pady=5, padx=10, fill='x')

    var_season_over_series = ctk.IntVar(value=season_over_series)
    checkbox_season_over_series = ctk.CTkCheckBox(
        master=col2, text='Prefer Season Poster Over Series', variable=var_season_over_series
    )
    checkbox_season_over_series.pack(anchor='w', pady=5, padx=10, fill='x')

    var_textless_posters = ctk.IntVar(value=textless_posters)
    checkbox_textless_posters = ctk.CTkCheckBox(
        master=col2, text='Prefer Textless TMDB Posters', variable=var_textless_posters
    )
    checkbox_textless_posters.pack(anchor='w', pady=5, padx=10, fill='x')

    label_cover_settings = ctk.CTkLabel(master=col2, text='Album Cover Settings', font=font_header)
    label_cover_settings.pack(pady=(10, 0), padx=10)

    var_always_use_musicbrainz = ctk.IntVar(value=always_use_musicbrainz)
    checkbox_always_use_musicbrainz = ctk.CTkCheckBox(
        master=col2, text='Always Use The Cover Art Archive', variable=var_always_use_musicbrainz
    )
    checkbox_always_use_musicbrainz.pack(anchor='w', pady=5, padx=10, fill='x')

    var_release_over_group = ctk.IntVar(value=release_over_group)
    checkbox_release_over_group = ctk.CTkCheckBox(
        master=col2, text='Prefer Release Cover Over Group', variable=var_release_over_group
    )
    checkbox_release_over_group.pack(anchor='w', pady=5, padx=10, fill='x')

    label_media_settings = ctk.CTkLabel(master=col2, text='Media Settings', font=font_header)
    label_media_settings.pack(pady=(10, 0), padx=10)

    media_types = parse_delimited_list(config, 'MEDIA_TYPES')
    var_movies = ctk.IntVar(value=int('Movies' in media_types))
    checkbox_movies = ctk.CTkCheckBox(
        master=col2, text='Show Watching Activity for Movies', variable=var_movies
    )
    checkbox_movies.pack(anchor='w', pady=5, padx=10, fill='x')

    var_shows = ctk.IntVar(value=int('Shows' in media_types))
    checkbox_shows = ctk.CTkCheckBox(
        master=col2, text='Show Watching Activity for Shows', variable=var_shows
    )
    checkbox_shows.pack(anchor='w', pady=5, padx=10, fill='x')

    var_music = ctk.IntVar(value=int('Music' in media_types))
    checkbox_music = ctk.CTkCheckBox(
        master=col2, text='Show Listening Activity for Music', variable=var_music
    )
    checkbox_music.pack(anchor='w', pady=5, padx=10, fill='x')

    label_activity_settings = ctk.CTkLabel(master=col3, text='Activity Settings', font=font_header)
    label_activity_settings.pack(pady=(10, 0), padx=10)

    var_paused = ctk.IntVar(value=show_when_paused)
    checkbox_paused = ctk.CTkCheckBox(
        master=col3, text='Show Activity While Paused', variable=var_paused
    )
    checkbox_paused.pack(anchor='w', pady=5, padx=10, fill='x')

    var_server_name = ctk.IntVar(value=show_server_name)
    checkbox_server_name = ctk.CTkCheckBox(
        master=col3, text='Show Jellyfin Server Name (Title)', variable=var_server_name
    )
    checkbox_server_name.pack(anchor='w', pady=5, padx=10, fill='x')

    var_jf_logo = ctk.IntVar(value=show_jf_logo)
    checkbox_jf_logo = ctk.CTkCheckBox(
        master=col3, text='Show Jellyfin Logo (Small Image)', variable=var_jf_logo
    )
    checkbox_jf_logo.pack(anchor='w', pady=5, padx=10, fill='x')

    label_system_settings = ctk.CTkLabel(master=col3, text='System Settings', font=font_header)
    label_system_settings.pack(pady=(10, 0), padx=10)

    if sys.platform == 'win32':
        var_startup_status = ctk.IntVar(value=int(get_startup_status()))
        checkbox_startup_status = ctk.CTkCheckBox(
            master=col3,
            text='Open Jellyfin RPC on Startup',
            variable=var_startup_status,
            command=lambda: set_startup_status(bool(var_startup_status.get())),
        )
        checkbox_startup_status.pack(anchor='w', pady=5, padx=10, fill='x')

    var_start_minimized = ctk.IntVar(value=start_minimized)
    checkbox_start_minimized = ctk.CTkCheckBox(
        master=col3, text='Start Minimized (If Connected)', variable=var_start_minimized
    )
    checkbox_start_minimized.pack(anchor='w', pady=5, padx=10, fill='x')

    background_type = 'Dock' if sys.platform == 'darwin' else 'Tray'
    var_minimize_on_close = ctk.IntVar(value=minimize_on_close)
    checkbox_minimize_on_close = ctk.CTkCheckBox(
        master=col3,
        text=f'Close Button Minimizes to {background_type}',
        variable=var_minimize_on_close,
    )
    checkbox_minimize_on_close.pack(anchor='w', pady=5, padx=10, fill='x')

    label_advanced_settings = ctk.CTkLabel(master=col3, text='Advanced Settings', font=font_header)
    label_advanced_settings.pack(pady=(10, 0), padx=10)

    frame_advanced_settings = ctk.CTkFrame(master=col3, fg_color='transparent')
    frame_advanced_settings.pack(fill='x', padx=10, pady=5)
    frame_advanced_settings.grid_columnconfigure(0, weight=1)
    frame_advanced_settings.grid_columnconfigure(1, weight=0)
    frame_advanced_settings.grid_columnconfigure(2, weight=0)
    frame_advanced_settings.grid_columnconfigure(3, weight=0)

    label_polling_rate = ctk.CTkLabel(master=frame_advanced_settings, text='Polling Rate:')
    label_polling_rate.grid(row=0, column=0, pady=5, sticky='w')

    button_polling_rate_dec = ctk.CTkButton(
        master=frame_advanced_settings, text='-', width=28, height=28
    )
    button_polling_rate_dec.grid(row=0, column=1, padx=(0, 5), pady=5, sticky='e')

    var_polling_rate = ctk.StringVar(value=f'{polling_rate}s')
    entry_polling_rate = ctk.CTkEntry(
        master=frame_advanced_settings, textvariable=var_polling_rate, width=50, justify='center'
    )
    entry_polling_rate.configure(state='disabled')
    entry_polling_rate.grid(row=0, column=2, pady=5, sticky='e')

    button_polling_rate_inc = ctk.CTkButton(
        master=frame_advanced_settings, text='+', width=28, height=28
    )
    button_polling_rate_inc.grid(row=0, column=3, padx=(5, 0), pady=5, sticky='e')

    label_seek_threshold = ctk.CTkLabel(master=frame_advanced_settings, text='Seek Threshold:')
    label_seek_threshold.grid(row=1, column=0, pady=5, sticky='w')

    button_seek_threshold_dec = ctk.CTkButton(
        master=frame_advanced_settings, text='-', width=28, height=28
    )
    button_seek_threshold_dec.grid(row=1, column=1, padx=(0, 5), pady=5, sticky='e')

    var_seek_threshold = ctk.StringVar(value=f'{seek_threshold}s')
    entry_seek_threshold = ctk.CTkEntry(
        master=frame_advanced_settings, textvariable=var_seek_threshold, width=50, justify='center'
    )
    entry_seek_threshold.configure(state='disabled')
    entry_seek_threshold.grid(row=1, column=2, pady=5, sticky='e')

    button_seek_threshold_inc = ctk.CTkButton(
        master=frame_advanced_settings, text='+', width=28, height=28
    )
    button_seek_threshold_inc.grid(row=1, column=3, padx=(5, 0), pady=5, sticky='e')

    label_log_level = ctk.CTkLabel(master=frame_advanced_settings, text='Log Level:')
    label_log_level.grid(row=2, column=0, pady=5, sticky='w')

    values_log_level = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    var_log_level = ctk.StringVar(value=log_level)
    optionmenu_log_level = ctk.CTkOptionMenu(
        master=frame_advanced_settings, values=values_log_level, variable=var_log_level, width=0
    )
    optionmenu_log_level.grid(row=2, column=1, columnspan=3, pady=5, sticky='ew')

    frame_open_buttons = ctk.CTkFrame(master=col3, fg_color='transparent')
    frame_open_buttons.pack(anchor='center')
    frame_open_buttons.grid_columnconfigure((0, 1), weight=1, uniform='open_buttons')

    button_open_ini = ctk.CTkButton(
        master=frame_open_buttons, text='Open INI', width=100, command=lambda: open_file(ini_path)
    )
    button_open_ini.grid(row=0, column=0, padx=5, sticky='ew')

    button_open_log = ctk.CTkButton(
        master=frame_open_buttons, text='Open Log', width=100, command=lambda: open_file(log_path)
    )
    button_open_log.grid(row=0, column=1, padx=5, sticky='ew')

    RPCLogger(frame_main, log_queue, textbox_status_monitor)
    rpc_process = RPCProcess(functools.partial(start_discord_rpc, ini_path, log_path), log_queue)

    global button_connect_text
    button_connect_text = 'Connect'

    class AppContext(TypedDict):
        button_connect: ctk.CTkButton | None
        tray_icon: pystray._base.Icon | None

    context: AppContext = {'button_connect': None, 'tray_icon': None}
    entries = {
        'JELLYFIN_HOST': {'entry': entry_jf_host, 'obfuscate': False},
        'JELLYFIN_API_KEY': {'entry': entry_jf_api_key, 'obfuscate': True},
        'JELLYFIN_USERNAME': {'entry': entry_jf_username, 'obfuscate': False},
        'TMDB_API_KEY': {'entry': entry_tmdb_api_key, 'obfuscate': True},
        'POSTER_LANGUAGES': {'entry': entry_languages, 'obfuscate': False},
    }
    checkboxes = {
        'MOVIES': checkbox_movies,
        'SHOWS': checkbox_shows,
        'MUSIC': checkbox_music,
        'START_MINIMIZED': checkbox_start_minimized,
        'MINIMIZE_ON_CLOSE': checkbox_minimize_on_close,
        'SEASON_OVER_SERIES': checkbox_season_over_series,
        'RELEASE_OVER_GROUP': checkbox_release_over_group,
        'SHOW_WHEN_PAUSED': checkbox_paused,
        'SHOW_SERVER_NAME': checkbox_server_name,
        'SHOW_JELLYFIN_LOGO': checkbox_jf_logo,
        'ALWAYS_USE_TMDB': checkbox_always_use_tmdb,
        'ALWAYS_USE_MUSICBRAINZ': checkbox_always_use_musicbrainz,
        'TEXTLESS_POSTERS': checkbox_textless_posters,
    }

    for key, checkbox in checkboxes.items():
        if key == 'MINIMIZE_ON_CLOSE':
            checkbox.configure(
                command=lambda: set_close_behavior(
                    root, on_close_callback, checkboxes['MINIMIZE_ON_CLOSE']._variable.get()
                )
            )
        elif key != 'START_MINIMIZED':
            checkbox.configure(
                command=lambda: on_click(
                    cast(ctk.CTkButton, context['button_connect']),
                    entries,
                    rpc_process,
                    only_disconnect=True,
                )
            )

    def set_log_level(level: str) -> None:
        logger.setLevel(level)
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    def inc_polling_rate() -> None:
        value = int(var_polling_rate.get().rstrip('s'))
        var_polling_rate.set(f'{value + 1}s')
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    def dec_polling_rate() -> None:
        value = int(var_polling_rate.get().rstrip('s'))
        if value > 1:
            var_polling_rate.set(f'{value - 1}s')
        else:
            var_polling_rate.set('1s')
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    def inc_seek_threshold() -> None:
        value = int(var_seek_threshold.get().rstrip('s'))
        var_seek_threshold.set(f'{value + 1}s')
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    def dec_seek_threshold() -> None:
        value = int(var_seek_threshold.get().rstrip('s'))
        if value > 1:
            var_seek_threshold.set(f'{value - 1}s')
        else:
            var_seek_threshold.set('1s')
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            only_disconnect=True,
        )

    optionmenu_log_level.configure(command=set_log_level)
    button_polling_rate_inc.configure(command=inc_polling_rate)
    button_polling_rate_dec.configure(command=dec_polling_rate)
    button_seek_threshold_inc.configure(command=inc_seek_threshold)
    button_seek_threshold_dec.configure(command=dec_seek_threshold)

    def on_click_callback() -> None:
        save_config(
            ini_path,
            entries,
            checkboxes,
            var_filter_mode,
            var_filter_libraries,
            var_log_level,
            var_polling_rate,
            var_seek_threshold,
        )
        on_click(
            cast(ctk.CTkButton, context['button_connect']),
            entries,
            rpc_process,
            context['tray_icon'],
        )

    def on_close_callback() -> None:
        save_config(
            ini_path,
            entries,
            checkboxes,
            var_filter_mode,
            var_filter_libraries,
            var_log_level,
            var_polling_rate,
            var_seek_threshold,
        )
        on_close(root, rpc_process, context['tray_icon'])

    tray_icon = None
    gui_queue: queue.Queue[str] = queue.Queue()
    if sys.platform == 'darwin':
        root.createcommand(
            '::tk::mac::ReopenApplication',
            lambda: on_maximize(label_update, frame_grid, frame_bottom, root),
        )
    elif sys.platform == 'win32':
        tray_icon = pystray.Icon(
            'jellyfin-rpc',
            Image.open(png_bundle_path),
            'Jellyfin RPC',
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: button_connect_text, lambda: gui_queue.put('CONNECT')),
                pystray.MenuItem('Maximize', lambda: gui_queue.put('MAXIMIZE'), default=True),
                pystray.MenuItem('Quit', lambda: gui_queue.put('QUIT')),
            ),
        )
        tray_icon.run_detached()
    context['tray_icon'] = tray_icon

    button_connect = ctk.CTkButton(
        master=frame_bottom, text=button_connect_text, command=on_click_callback
    )
    button_connect.pack(pady=(5, 10))
    context['button_connect'] = button_connect
    if jf_host:  # and jf_api_key and jf_username:
        on_click_callback()
        if start_minimized and button_connect_text == 'Disconnect':
            if sys.platform == 'linux':
                root.iconify()
            else:
                root.withdraw()
    else:
        logger.info('Open Setup Guide')

    def poll_process_status() -> None:
        status_text = textbox_status_monitor.get('1.0', 'end')
        if not var_jf_api_key.get() and 'via Quick Connect' in status_text:
            config = load_config(ini_path)
            var_jf_api_key.set(config.get('JELLYFIN_API_KEY', ''))
            var_jf_username.set(config.get('JELLYFIN_USERNAME', ''))
        if rpc_process.has_failed():
            on_click_callback()
        root.after(1000, lambda: poll_process_status())

    poll_process_status()

    def poll_gui_queue() -> None:
        try:
            match gui_queue.get_nowait():
                case 'CONNECT':
                    on_click_callback()
                case 'MAXIMIZE':
                    on_maximize(label_update, frame_grid, frame_bottom, root)
                case 'QUIT':
                    on_close_callback()
        except queue.Empty:
            pass
        finally:
            root.after(100, lambda: poll_gui_queue())

    if tray_icon:
        poll_gui_queue()

    if sys.platform == 'win32':
        root.iconbitmap(ico_bundle_path)
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
    root.resizable(True, True)

    set_close_behavior(root, on_close_callback, minimize_on_close)
    root.mainloop()


if __name__ == '__main__':
    mp.freeze_support()
    main()
