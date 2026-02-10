import functools
import logging
import multiprocessing as mp
import os
import platform
import queue
import shutil
import sys
import threading
import webbrowser
from configparser import ConfigParser
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from typing import Callable, TypedDict, cast

import customtkinter as ctk
import pystray
import requests
from PIL import Image
from requests.exceptions import RequestException

from jellyfin_rpc import __version__, load_config, start_discord_rpc

button1_text = ''
logger = logging.getLogger('GUI')


class RPCProcess:
    def __init__(self, target: Callable, log_queue: mp.Queue):
        self.target = target
        self.log_queue = log_queue
        self.process: mp.Process | None = None

    def start(self):
        self.process = mp.Process(target=self.target, args=(self.log_queue,))
        self.process.start()

    def stop(self):
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
    def __init__(self, frame: ctk.CTkFrame, log_queue: mp.Queue, text_widget: ctk.CTkTextbox):
        self.frame = frame
        self.log_queue = log_queue
        self.text_widget = text_widget
        self.frame.after(100, self.poll_log_queue)

    def poll_log_queue(self):
        while not self.log_queue.empty():
            record = self.log_queue.get_nowait()
            self.display_record(record)
        self.frame.after(100, self.poll_log_queue)

    def display_record(self, record: LogRecord):
        message = self.format_log_record(record)
        self.text_widget.configure(state='normal')
        self.text_widget.insert(ctk.END, message)
        if message.rstrip().endswith('Need Help?'):
            tk_text = self.text_widget._textbox
            tk_text.tag_add('link', '1.6', '1.16')
            mode_index = 0 if ctk.get_appearance_mode() == 'Light' else 1
            link_color = ctk.ThemeManager.theme['CTkButton']['fg_color'][mode_index]
            tk_text.tag_configure('link', foreground=link_color, underline=True)
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

    def format_log_record(self, record: LogRecord):
        return f'{record.levelname}: {record.getMessage()}\n'


def save_config(
    ini_path: str,
    entries: dict[str, ctk.CTkEntry],
    checkboxes: dict[str, ctk.CTkCheckBox],
    log_level_var: ctk.StringVar,
    refresh_rate_var: ctk.StringVar,
):
    config = ConfigParser()
    config.read(ini_path)
    for key in (
        'JELLYFIN_HOST',
        'JELLYFIN_API_KEY',
        'JELLYFIN_USERNAME',
        'TMDB_API_KEY',
        'POSTER_LANGUAGES',
    ):
        config.set('DEFAULT', key, entries[key].get())
    config.set('DEFAULT', 'LOG_LEVEL', log_level_var.get())
    config.set('DEFAULT', 'REFRESH_RATE', refresh_rate_var.get().rstrip('s'))

    media_types = []
    if checkboxes['MOVIES']._variable.get():
        media_types.append('Movies')
    if checkboxes['SHOWS']._variable.get():
        media_types.append('Shows')
    if checkboxes['MUSIC']._variable.get():
        media_types.append('Music')
    config.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))

    for key in (
        'START_MINIMIZED',
        'MINIMIZE_ON_CLOSE',
        'SEASON_OVER_SERIES',
        'RELEASE_OVER_GROUP',
        'FIND_BEST_MATCH',
        'SHOW_WHEN_PAUSED',
        'SHOW_SERVER_NAME',
        'SHOW_JELLYFIN_ICON',
    ):
        config.set('DEFAULT', key, str(bool(checkboxes[key]._variable.get())).lower())

    with open(ini_path, 'w') as ini_file:
        config.write(ini_file)


def on_click(
    button1: ctk.CTkButton,
    entries: dict[str, ctk.CTkEntry],
    rpc_process: RPCProcess,
    tray_icon: pystray._base.Icon | None = None,
    only_disconnect: bool = False,
):
    global button1_text
    if button1_text == 'Connect' and not only_disconnect:
        rpc_process.start()
        for entry in entries.values():
            entry.configure(state='readonly')
            entry.update()
        button1_text = 'Disconnect'
        button1.configure(text=button1_text)
    else:
        rpc_process.stop()
        for entry in entries.values():
            entry.configure(state='normal')
            entry.update()
        button1_text = 'Connect'
        button1.configure(text=button1_text)
    if tray_icon is not None:
        tray_icon.update_menu()
    button1.update()


def on_maximize(label: ctk.CTkLabel, root: ctk.CTk | None = None):
    threading.Thread(target=check_version, args=(label,), daemon=True).start()
    if root is not None:
        root.after(0, root.deiconify)


def on_close(root: ctk.CTk, rpc_process: RPCProcess, tray_icon: pystray._base.Icon | None = None):
    rpc_process.stop()
    if tray_icon is not None:
        tray_icon.visible = False
        tray_icon.stop()
    root.quit()


def set_close_behavior(root: ctk.CTk, on_close_callback: Callable, withdraw: bool):
    if withdraw:
        root.protocol('WM_DELETE_WINDOW', root.withdraw)
    else:
        root.protocol('WM_DELETE_WINDOW', on_close_callback)


def get_executable_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    else:
        return os.path.abspath(__file__)


if platform.system() == 'Windows':
    import winreg

    def set_startup_status(enabled: bool):
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


def parse_version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.lstrip('v').split('.'))


def check_version(label: ctk.CTkLabel):
    mode_index = 0 if ctk.get_appearance_mode() == 'Light' else 1
    try:
        response = requests.get(
            'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest', timeout=5
        )
        response.raise_for_status()
        latest_version = parse_version(response.json()['tag_name'])
        if parse_version(__version__) >= latest_version:
            label_text = f'Latest Version ({__version__})'
            label_color = ctk.ThemeManager.theme['CTkLabel']['text_color'][mode_index]
        else:
            label_text = f'Update Available ({__version__} \u2192 {latest_version})'
            label_color = ctk.ThemeManager.theme['CTkButton']['fg_color'][mode_index]
    except (RequestException, JSONDecodeError, KeyError) as e:
        logger.debug(e)
        logger.warning('Connection to GitHub Failed. Skipping Version Check...')
        label_text = f'Current Version ({__version__})'
        label_color = ctk.ThemeManager.theme['CTkLabel']['text_color'][mode_index]
    label.after(0, lambda: label.configure(text=label_text, text_color=label_color))


def main():
    ctk.set_appearance_mode('system')
    ctk.deactivate_automatic_dpi_awareness()

    root = ctk.CTk()
    root.title('Jellyfin RPC')
    root.geometry('285x488')
    gui_queue: queue.Queue[str] = queue.Queue()

    main_frame = ctk.CTkFrame(master=root)
    main_frame.pack(fill='both', expand=True)
    font = ctk.CTkFont(family='Roboto', size=14, weight='bold')

    ini_name, log_name = 'jellyfin_rpc.ini', 'jellyfin_rpc.log'
    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_bundle_path = os.path.abspath(os.path.join(bundle_dir, ini_name))
    png_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    os.chdir(os.path.dirname(get_executable_path()))

    data_dir = ''
    if platform.system() == 'Windows':
        data_dir = os.getenv('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
    elif platform.system() == 'Darwin':
        data_dir = os.path.expanduser('~/Library/Application Support')
    if data_dir:
        data_dir = os.path.join(data_dir, 'Jellyfin RPC')
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
    jf_host = config.get('JELLYFIN_HOST')
    jf_api_key = config.get('JELLYFIN_API_KEY')
    jf_username = config.get('JELLYFIN_USERNAME')
    log_level = config.get('LOG_LEVEL', 'INFO').upper()
    refresh_rate = max(1, config.getint('REFRESH_RATE', 5))
    poster_languages = config.get('POSTER_LANGUAGES')
    season_over_series = config.getboolean('SEASON_OVER_SERIES', True)
    release_over_group = config.getboolean('RELEASE_OVER_GROUP', True)
    find_best_match = config.getboolean('FIND_BEST_MATCH', True)
    start_minimized = config.getboolean('START_MINIMIZED', True)
    minimize_on_close = config.getboolean('MINIMIZE_ON_CLOSE', True)
    show_server_name = config.getboolean('SHOW_SERVER_NAME', False)
    show_when_paused = config.getboolean('SHOW_WHEN_PAUSED', True)
    show_jf_icon = config.getboolean('SHOW_JELLYFIN_ICON', False)

    label1 = ctk.CTkLabel(master=main_frame, text='Checking for Update...', cursor='hand2')
    label1.bind(
        '<Button-1>',
        lambda _: webbrowser.open_new_tab('https://github.com/kennethsible/jellyfin-rpc/releases'),
    )
    label1.pack(pady=(5, 0), padx=10)
    on_maximize(label1)

    scroll_frame = ctk.CTkScrollableFrame(master=main_frame, fg_color=main_frame.cget('fg_color'))
    scroll_frame.pack(fill='both', expand=True)

    entry1_text = ctk.StringVar(value=jf_host)
    entry1 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry1_text if entry1_text.get() else None,
        placeholder_text='Jellyfin Host',
        width=265,
    )
    entry1.pack(pady=(0, 5), padx=10)

    entry2_text = ctk.StringVar(value=jf_api_key)
    entry2 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry2_text if entry2_text.get() else None,
        placeholder_text='Jellyfin API Key',
        width=265,
    )
    entry2.pack(pady=5, padx=10)

    entry3_text = ctk.StringVar(value=jf_username)
    entry3 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry3_text if entry3_text.get() else None,
        placeholder_text='Jellyfin Username',
        width=265,
    )
    entry3.pack(pady=5, padx=10)

    entry4_text = ctk.StringVar(value=config.get('TMDB_API_KEY'))
    entry4 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry4_text if entry4_text.get() else None,
        placeholder_text='TMDB API Key (Optional)',
        width=265,
    )
    entry4.pack(pady=5, padx=10)

    textbox1 = ctk.CTkTextbox(master=scroll_frame, width=265, height=100)
    textbox1.configure(state='disabled')
    textbox1.pack(pady=5, padx=10)

    log_queue: mp.Queue[LogRecord] = mp.Queue()
    logger.setLevel(log_level)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    file_hdlr = logging.FileHandler(log_path, encoding='utf-8')
    file_hdlr.setFormatter(formatter)
    stream_hdlr = logging.StreamHandler(sys.stdout)
    stream_hdlr.setFormatter(formatter)
    queue_hdlr = handlers.QueueHandler(log_queue)
    for hdlr in (file_hdlr, stream_hdlr, queue_hdlr):
        logger.addHandler(hdlr)
    RPCLogger(main_frame, log_queue, textbox1)

    checkbox_container1 = ctk.CTkFrame(master=scroll_frame, fg_color='transparent')
    checkbox_container1.pack(pady=5, padx=10)

    label2 = ctk.CTkLabel(master=checkbox_container1, text='System Settings', font=font)
    label2.pack(pady=(5, 0), padx=10)

    if platform.system() == 'Windows':
        checkbox4_var = ctk.IntVar(value=int(get_startup_status()))
        checkbox4 = ctk.CTkCheckBox(
            master=checkbox_container1,
            text='Open Jellyfin RPC on Startup',
            variable=checkbox4_var,
            command=lambda: set_startup_status(bool(checkbox4_var.get())),
        )
        checkbox4.pack(anchor='w', pady=5)

    checkbox5_var = ctk.IntVar(value=start_minimized)
    checkbox5 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Start Minimized (If Connected)', variable=checkbox5_var
    )
    checkbox5.pack(anchor='w', pady=5)

    background_type = 'Dock' if platform.system() == 'Darwin' else 'Tray'
    checkbox6_var = ctk.IntVar(value=minimize_on_close)
    checkbox6 = ctk.CTkCheckBox(
        master=checkbox_container1,
        text=f'Close Button Minimizes to {background_type}',
        variable=checkbox6_var,
    )
    checkbox6.pack(anchor='w', pady=5)

    label3 = ctk.CTkLabel(master=checkbox_container1, text='Media Settings', font=font)
    label3.pack(pady=(5, 0), padx=10)

    media_types = config.get('MEDIA_TYPES', 'Movies,Shows,Music').split(',')
    checkbox1_var = ctk.IntVar(value=int('Movies' in media_types))
    checkbox1 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Watching for Movies', variable=checkbox1_var
    )
    checkbox1.pack(anchor='w', pady=5)

    checkbox2_var = ctk.IntVar(value=int('Shows' in media_types))
    checkbox2 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Watching for Shows', variable=checkbox2_var
    )
    checkbox2.pack(anchor='w', pady=5)

    checkbox3_var = ctk.IntVar(value=int('Music' in media_types))
    checkbox3 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Listening for Music', variable=checkbox3_var
    )
    checkbox3.pack(anchor='w', pady=5)

    label8 = ctk.CTkLabel(master=checkbox_container1, text='Image Settings', font=font)
    label8.pack(pady=(5, 0), padx=10)

    poster_container1 = ctk.CTkFrame(master=checkbox_container1, fg_color='transparent')
    poster_container1.pack(fill='x', padx=10, pady=5)

    label9 = ctk.CTkLabel(master=poster_container1, text='Poster Language(s):')
    label9.pack(side='left', padx=(0, 10))

    entry6_text = ctk.StringVar(value=poster_languages)
    entry6 = ctk.CTkEntry(
        master=poster_container1,
        textvariable=entry6_text if entry6_text.get() else None,
        placeholder_text='e.g., en ja',
        width=265,
    )
    entry6.pack(side='right', fill='x', expand=True)

    checkbox10_var = ctk.IntVar(value=season_over_series)
    checkbox10 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Prefer Season Poster Over Series', variable=checkbox10_var
    )
    checkbox10.pack(anchor='w', pady=5)

    checkbox11_var = ctk.IntVar(value=release_over_group)
    checkbox11 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Prefer Release Cover Over Group', variable=checkbox11_var
    )
    checkbox11.pack(anchor='w', pady=5)

    checkbox12_var = ctk.IntVar(value=find_best_match)
    checkbox12 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Find Best Match for Missing IDs', variable=checkbox12_var
    )
    checkbox12.pack(anchor='w', pady=5)

    label4 = ctk.CTkLabel(master=checkbox_container1, text='Activity Settings', font=font)
    label4.pack(pady=(5, 0), padx=10)

    checkbox7_var = ctk.IntVar(value=show_when_paused)
    checkbox7 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Activity When Paused', variable=checkbox7_var
    )
    checkbox7.pack(anchor='w', pady=5)

    checkbox8_var = ctk.IntVar(value=show_server_name)
    checkbox8 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Server Name in Activity', variable=checkbox8_var
    )
    checkbox8.pack(anchor='w', pady=5)

    checkbox9_var = ctk.IntVar(value=show_jf_icon)
    checkbox9 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Jellyfin Icon in Activity', variable=checkbox9_var
    )
    checkbox9.pack(anchor='w', pady=5)

    label5 = ctk.CTkLabel(master=checkbox_container1, text='Advanced Settings', font=font)
    label5.pack(pady=(5, 0), padx=10)

    advanced_container1 = ctk.CTkFrame(master=checkbox_container1, fg_color='transparent')
    advanced_container1.pack(fill='x', padx=10, pady=5)

    label6 = ctk.CTkLabel(master=advanced_container1, text='Log Level:')
    label6.pack(side='left', padx=(0, 10))

    log_level_var = ctk.StringVar(value=log_level)
    optionmenu1 = ctk.CTkOptionMenu(
        master=advanced_container1, values=['INFO', 'DEBUG'], variable=log_level_var
    )
    optionmenu1.pack(side='right', fill='x', expand=True)

    advanced_container2 = ctk.CTkFrame(master=checkbox_container1, fg_color='transparent')
    advanced_container2.pack(fill='x', padx=10, pady=5)

    label7 = ctk.CTkLabel(master=advanced_container2, text='Refresh Rate:')
    label7.pack(side='left', padx=(0, 10))

    refresh_rate_var = ctk.StringVar(value=f'{refresh_rate}s')

    button2 = ctk.CTkButton(master=advanced_container2, text='+', width=28, height=28)
    button2.pack(side='right', padx=(5, 0))

    entry5 = ctk.CTkEntry(
        master=advanced_container2, textvariable=refresh_rate_var, width=50, justify='center'
    )
    entry5.configure(state='disabled')
    entry5.pack(side='right')

    button3 = ctk.CTkButton(master=advanced_container2, text='-', width=28, height=28)
    button3.pack(side='right', padx=(0, 5))

    rpc_process = RPCProcess(functools.partial(start_discord_rpc, ini_path, log_path), log_queue)
    global button1_text
    button1_text = 'Connect'

    class AppContext(TypedDict):
        button1: ctk.CTkButton | None
        tray_icon: pystray._base.Icon | None

    context: AppContext = {'button1': None, 'tray_icon': None}
    entries = {
        'JELLYFIN_HOST': entry1,
        'JELLYFIN_API_KEY': entry2,
        'JELLYFIN_USERNAME': entry3,
        'TMDB_API_KEY': entry4,
        'POSTER_LANGUAGES': entry6,
    }
    checkboxes = {
        'MOVIES': checkbox1,
        'SHOWS': checkbox2,
        'MUSIC': checkbox3,
        'START_MINIMIZED': checkbox5,
        'MINIMIZE_ON_CLOSE': checkbox6,
        'SEASON_OVER_SERIES': checkbox10,
        'RELEASE_OVER_GROUP': checkbox11,
        'FIND_BEST_MATCH': checkbox12,
        'SHOW_WHEN_PAUSED': checkbox7,
        'SHOW_SERVER_NAME': checkbox8,
        'SHOW_JELLYFIN_ICON': checkbox9,
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
                    cast(ctk.CTkButton, context['button1']),
                    entries,
                    rpc_process,
                    only_disconnect=True,
                )
            )

    def set_log_level(level: str):
        logger.setLevel(level)
        on_click(
            cast(ctk.CTkButton, context['button1']), entries, rpc_process, only_disconnect=True
        )

    def inc_refresh():
        value = int(refresh_rate_var.get().rstrip('s'))
        refresh_rate_var.set(f'{value + 1}s')
        on_click(
            cast(ctk.CTkButton, context['button1']), entries, rpc_process, only_disconnect=True
        )

    def dec_refresh():
        value = int(refresh_rate_var.get().rstrip('s'))
        if value > 1:
            refresh_rate_var.set(f'{value - 1}s')
        else:
            refresh_rate_var.set('1s')
        on_click(
            cast(ctk.CTkButton, context['button1']), entries, rpc_process, only_disconnect=True
        )

    optionmenu1.configure(command=set_log_level)
    button2.configure(command=inc_refresh)
    button3.configure(command=dec_refresh)

    def on_click_callback():
        save_config(ini_path, entries, checkboxes, log_level_var, refresh_rate_var)
        on_click(
            cast(ctk.CTkButton, context['button1']), entries, rpc_process, context['tray_icon']
        )

    def on_close_callback():
        save_config(ini_path, entries, checkboxes, log_level_var, refresh_rate_var)
        on_close(root, rpc_process, context['tray_icon'])

    if platform.system() == 'Darwin':
        tray_icon = None
        root.createcommand('::tk::mac::ReopenApplication', lambda: on_maximize(label1, root))
    else:
        tray_icon = pystray.Icon(
            'jellyfin-rpc',
            Image.open(png_bundle_path),
            'Jellyfin RPC',
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: button1_text, lambda: gui_queue.put('CONNECT')),
                pystray.MenuItem('Maximize', lambda: gui_queue.put('MAXIMIZE'), default=True),
                pystray.MenuItem('Quit', lambda: gui_queue.put('QUIT')),
            ),
        )
        tray_icon.run_detached()
    context['tray_icon'] = tray_icon

    button1 = ctk.CTkButton(master=main_frame, text=button1_text, command=on_click_callback)
    button1.pack(side='bottom', pady=(5, 10), padx=10)
    context['button1'] = button1
    if jf_host and jf_api_key and jf_username:
        on_click_callback()
        if start_minimized and button1_text == 'Disconnect':
            root.withdraw()
    else:
        logger.info('Need Help?')

    def poll_process_status():
        if rpc_process.has_failed():
            on_click_callback()
        root.after(1000, lambda: poll_process_status())

    poll_process_status()

    def poll_gui_queue():
        try:
            match gui_queue.get_nowait():
                case 'CONNECT':
                    on_click_callback()
                case 'MAXIMIZE':
                    on_maximize(label1, root)
                case 'QUIT':
                    on_close_callback()
        except queue.Empty:
            pass
        finally:
            root.after(100, lambda: poll_gui_queue())

    poll_gui_queue()

    if platform.system() == 'Windows':
        root.iconbitmap(ico_bundle_path)
    root.resizable(False, False)
    set_close_behavior(root, on_close_callback, minimize_on_close)
    root.mainloop()


if __name__ == '__main__':
    mp.freeze_support()
    main()
