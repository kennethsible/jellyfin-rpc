import functools
import logging
import multiprocessing
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
from multiprocessing.queues import Queue
from typing import Callable, Optional, TypedDict

import customtkinter as ctk
import pystray
import requests
from PIL import Image
from requests.exceptions import RequestException

import jellyfin_rpc

__version__ = '1.6.0'

logger = logging.getLogger('GUI')


class RPCProcess:
    def __init__(self, target: Callable, log_queue: Queue):
        self.target = target
        self.log_queue = log_queue
        self.process: multiprocessing.Process | None = None

    def start(self):
        self.process = multiprocessing.Process(target=self.target, args=(self.log_queue,))
        self.process.start()

    def stop(self):
        if self.process is None:
            return
        if self.process.is_alive():
            self.process.terminate()
            self.process.join()

    def has_failed(self) -> bool:
        if self.process is None:
            return False
        if self.process.exitcode is None:
            return False
        if self.process.exitcode in (0, -15):
            logger.info('RPC Process Stopped')
            self.process = None
            return False
        logger.error('RPC Process Exited Unexpectedly')
        self.process = None
        return True


class RPCLogger:
    def __init__(
        self, frame: ctk.CTkFrame, log_queue: multiprocessing.Queue, text_widget: ctk.CTkTextbox
    ):
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
        self.text_widget.configure(state='disabled')
        self.text_widget.see(ctk.END)

    def format_log_record(self, record: LogRecord):
        return f'{record.levelname}: {record.getMessage()}\n'


def set_config(ini_path: str, entries: list[ctk.CTkEntry], checkboxes: list[ctk.CTkCheckBox]):
    config = ConfigParser()
    config.read(ini_path)

    config.set('DEFAULT', 'JELLYFIN_HOST', entries[0].get())
    config.set('DEFAULT', 'JELLYFIN_API_KEY', entries[1].get())
    config.set('DEFAULT', 'JELLYFIN_USERNAME', entries[2].get())
    config.set('DEFAULT', 'TMDB_API_KEY', entries[3].get())
    config.set('DEFAULT', 'START_MINIMIZED', str(checkboxes[3]._variable.get()))
    config.set('DEFAULT', 'MINIMIZE_ON_CLOSE', str(checkboxes[4]._variable.get()))
    config.set('DEFAULT', 'SHOW_WHEN_PAUSED', str(checkboxes[5]._variable.get()))
    config.set('DEFAULT', 'SHOW_SERVER_NAME', str(checkboxes[6]._variable.get()))
    config.set('DEFAULT', 'SHOW_JELLYFIN_ICON', str(checkboxes[7]._variable.get()))

    if not config.get('DEFAULT', 'LOG_LEVEL', fallback=None):
        config.set('DEFAULT', 'LOG_LEVEL', 'INFO')

    media_types = []
    if checkboxes[0]._variable.get():
        media_types.append('Movies')
    if checkboxes[1]._variable.get():
        media_types.append('Shows')
    if checkboxes[2]._variable.get():
        media_types.append('Music')
    config.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))

    with open(ini_path, 'w') as ini_file:
        config.write(ini_file)


def on_click(
    root: ctk.CTk,
    ini_path: str,
    rpc_process: RPCProcess,
    entries: list[ctk.CTkEntry],
    checkboxes: list[ctk.CTkCheckBox],
    button: ctk.CTkButton,
    icon: pystray._base.Icon | None = None,
):
    global button_text
    if button_text == 'Connect':
        set_config(ini_path, entries, checkboxes)
        rpc_process.start()
        for component in entries + checkboxes:
            component.configure(state='readonly')
            component.update()
        button_text = 'Disconnect'
        button.configure(text=button_text)
        if checkboxes[5]._variable.get():
            root.protocol('WM_DELETE_WINDOW', root.withdraw)
        else:
            root.protocol(
                'WM_DELETE_WINDOW',
                lambda: on_close(root, ini_path, rpc_process, entries, checkboxes, icon),
            )
    else:
        rpc_process.stop()
        for component in entries + checkboxes:
            component.configure(state='normal')
            component.update()
        button_text = 'Connect'
        button.configure(text=button_text)
    if icon:
        icon.update_menu()
    button.update()


def on_maximize(label: ctk.CTkLabel, root: ctk.CTk | None = None):
    threading.Thread(target=check_version, args=(label,), daemon=True).start()
    if root is not None:
        root.after(0, root.deiconify)


def on_close(
    root: ctk.CTk,
    ini_path: str,
    rpc_process: RPCProcess,
    entries: list[ctk.CTkEntry],
    checkboxes: list[ctk.CTkCheckBox],
    icon: pystray._base.Icon | None = None,
):
    try:
        set_config(ini_path, entries, checkboxes)
    except Exception:
        logger.exception('Error Writing Config')
    rpc_process.stop()
    if icon is not None:
        icon.visible = False
        icon.stop()
    root.quit()


def get_executable_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    else:
        return os.path.abspath(__file__)


if platform.system() == 'Windows':
    import winreg

    def set_startup_status(enabled: bool):
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0,
            winreg.KEY_SET_VALUE,
        )
        if enabled:
            exe_path = get_executable_path()
            winreg.SetValueEx(key, 'Jellyfin RPC', 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, 'Jellyfin RPC')
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)

    def get_startup_status() -> bool:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run',
                0,
                winreg.KEY_READ,
            )
            _, _ = winreg.QueryValueEx(key, 'Jellyfin RPC')
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False


def check_version(label: ctk.CTkLabel):
    try:
        response = requests.get(
            'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest', timeout=5
        )
        response.raise_for_status()
        release = response.json()['tag_name'].lstrip('v')
        if __version__ == release:
            label_text = f'Latest Version ({__version__})'
            label_color = 'gray'
        else:
            label_text = f'Update Available ({__version__} \u2192 {release})'
            label_color = 'white'
    except (RequestException, JSONDecodeError, KeyError):
        logger.warning('Connection to GitHub Failed')
        label_text = f'Current Version ({__version__})'
        label_color = 'gray'
    label.after(0, lambda: label.configure(text=label_text, text_color=label_color))


def main():
    ctk.set_appearance_mode('system')
    ctk.set_default_color_theme('dark-blue')
    ctk.deactivate_automatic_dpi_awareness()

    root = ctk.CTk()
    root.title('Jellyfin RPC')
    root.geometry('285x488')
    gui_queue = queue.Queue()

    main_frame = ctk.CTkFrame(master=root)
    main_frame.pack(fill='both', expand=True)
    font = ctk.CTkFont(family='Roboto', size=14, weight='bold')

    ini_path, log_path = 'jellyfin_rpc.ini', 'jellyfin_rpc.log'
    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_bundle_path = os.path.abspath(os.path.join(bundle_dir, ini_path))
    png_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_bundle_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    os.chdir(os.path.dirname(get_executable_path()))

    if not os.path.isfile(ini_path):
        shutil.copyfile(ini_bundle_path, ini_path)

    config = jellyfin_rpc.get_config(ini_path)
    jf_host = config.get('JELLYFIN_HOST')
    jf_api_key = config.get('JELLYFIN_API_KEY')
    jf_username = config.get('JELLYFIN_USERNAME')
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
    label1.pack(pady=(10, 0), padx=10)
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

    log_queue = multiprocessing.Queue()
    logger.setLevel(config['LOG_LEVEL'])
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

    rpc_process = RPCProcess(functools.partial(jellyfin_rpc.main), log_queue)
    global button_text
    button_text = 'Connect'

    class AppContext(TypedDict):
        button: Optional[ctk.CTkButton]
        icon: Optional[pystray._base.Icon]

    context: AppContext = {'button': None, 'icon': None}
    entries = [entry1, entry2, entry3, entry4]
    checkboxes = [
        checkbox1,
        checkbox2,
        checkbox3,
        checkbox5,
        checkbox6,
        checkbox7,
        checkbox8,
        checkbox9,
    ]

    def on_click_callback():
        assert context['button'] is not None, 'button is not initialized'
        on_click(
            root, ini_path, rpc_process, entries, checkboxes, context['button'], context['icon']
        )

    if platform.system() == 'Darwin':
        icon = None
        root.createcommand('::tk::mac::ReopenApplication', lambda: on_maximize(label1, root))
    else:
        icon = pystray.Icon(
            'jellyfin-rpc',
            Image.open(png_bundle_path),
            'Jellyfin RPC',
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: button_text, lambda: gui_queue.put('CONNECT')),
                pystray.MenuItem('Maximize', lambda: gui_queue.put('MAXIMIZE'), default=True),
                pystray.MenuItem('Quit', lambda: gui_queue.put('QUIT')),
            ),
        )
        icon.run_detached()
    context['icon'] = icon

    button = ctk.CTkButton(master=main_frame, text=button_text, command=on_click_callback)
    button.pack(side='bottom', pady=(5, 10), padx=10)
    context['button'] = button
    if jf_host and jf_api_key and jf_username:
        on_click_callback()
        if start_minimized and button_text == 'Disconnect':
            root.withdraw()
    else:
        logger.info('Awaiting Configuration')

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
                    on_close(root, ini_path, rpc_process, entries, checkboxes, icon)
        except queue.Empty:
            pass
        finally:
            root.after(100, lambda: poll_gui_queue())

    poll_gui_queue()

    if platform.system() == 'Windows':
        root.iconbitmap(ico_bundle_path)
    root.resizable(False, False)
    if minimize_on_close:
        root.protocol('WM_DELETE_WINDOW', root.withdraw)
    else:
        root.protocol(
            'WM_DELETE_WINDOW',
            lambda: on_close(root, ini_path, rpc_process, entries, checkboxes, icon),
        )
    root.mainloop()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
