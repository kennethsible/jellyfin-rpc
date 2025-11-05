import configparser
import functools
import logging
import multiprocessing
import os
import platform
import shutil
import sys
import threading
import webbrowser
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

logger = logging.getLogger(__name__)


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


def on_click(
    rpc_process: RPCProcess,
    ini_path: str,
    root: ctk.CTk,
    entry1: ctk.CTkEntry,
    entry2: ctk.CTkEntry,
    entry3: ctk.CTkEntry,
    entry4: ctk.CTkEntry,
    checkbox1: ctk.CTkCheckBox,
    checkbox2: ctk.CTkCheckBox,
    checkbox3: ctk.CTkCheckBox,
    checkbox5: ctk.CTkCheckBox,
    checkbox6: ctk.CTkCheckBox,
    checkbox7: ctk.CTkCheckBox,
    checkbox8: ctk.CTkCheckBox,
    checkbox9: ctk.CTkCheckBox,
    button1: ctk.CTkButton,
    icon: pystray._base.Icon | None = None,
):
    global button1_text
    if button1_text == 'Connect':
        config = configparser.ConfigParser()
        config.read(ini_path)
        config.set('DEFAULT', 'JELLYFIN_HOST', entry1.get())
        config.set('DEFAULT', 'JELLYFIN_API_KEY', entry2.get())
        config.set('DEFAULT', 'JELLYFIN_USERNAME', entry3.get())
        config.set('DEFAULT', 'TMDB_API_KEY', entry4.get())
        config.set('DEFAULT', 'START_MINIMIZED', str(checkbox5._variable.get()))
        config.set('DEFAULT', 'MINIMIZE_ON_CLOSE', str(checkbox6._variable.get()))
        config.set('DEFAULT', 'SHOW_SERVER_NAME', str(checkbox7._variable.get()))
        config.set('DEFAULT', 'SHOW_WHEN_PAUSED', str(checkbox8._variable.get()))
        config.set('DEFAULT', 'SHOW_JELLYFIN_ICON', str(checkbox9._variable.get()))
        if not config.get('DEFAULT', 'LOG_LEVEL', fallback=None):
            config.set('DEFAULT', 'LOG_LEVEL', 'INFO')
        media_types = []
        if checkbox1._variable.get():
            media_types.append('Movies')
        if checkbox2._variable.get():
            media_types.append('Shows')
        if checkbox3._variable.get():
            media_types.append('Music')
        config.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))
        with open(ini_path, 'w') as ini_file:
            config.write(ini_file)
        rpc_process.start()
        for entry in (
            entry1,
            entry2,
            entry3,
            entry4,
            checkbox1,
            checkbox2,
            checkbox3,
            checkbox5,
            checkbox6,
            checkbox7,
            checkbox8,
            checkbox9,
        ):
            entry.configure(state='readonly')
            entry.update()
        button1_text = 'Disconnect'
        button1.configure(text=button1_text)
        if checkbox6._variable.get():
            root.protocol('WM_DELETE_WINDOW', root.withdraw)
        else:
            root.protocol('WM_DELETE_WINDOW', lambda: on_close(rpc_process, root, icon))
    else:
        rpc_process.stop()
        for entry in (
            entry1,
            entry2,
            entry3,
            entry4,
            checkbox1,
            checkbox2,
            checkbox3,
            checkbox5,
            checkbox6,
            checkbox7,
            checkbox8,
            checkbox9,
        ):
            entry.configure(state='normal')
            entry.update()
        button1_text = 'Connect'
        button1.configure(text=button1_text)
    if icon:
        icon.update_menu()
    button1.update()


def on_maximize(label: ctk.CTkLabel, root: ctk.CTk | None = None):
    threading.Thread(target=check_version, args=(label,), daemon=True).start()
    if root is not None:
        root.after(0, root.deiconify)


def check_version(label: ctk.CTkLabel):
    try:
        response = requests.get(
            'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest', timeout=5
        )
        response.raise_for_status()
        release = response.json()['tag_name'].lstrip('v')
        if __version__ == release:
            label.configure(
                text=f'Latest Version ({__version__})',
                text_color='gray',
            )
        else:
            label.configure(
                text=f'Update Available ({__version__} \u2192 {release})',
                text_color='white',
            )
    except (RequestException, JSONDecodeError, KeyError):
        logger.warning('Connection to GitHub Failed')
        label.configure(
            text=f'Current Version ({__version__})',
            text_color='gray',
        )


def on_close(rpc_process: RPCProcess, root: ctk.CTk, icon: pystray._base.Icon | None = None):
    rpc_process.stop()
    if icon is not None:
        icon.visible = False
        icon.stop()
    root.quit()


def callback(url: str):
    webbrowser.open_new_tab(url)


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


def monitor_process_status(rpc_process: RPCProcess, on_click_partial: Callable, root: ctk.CTk):
    if rpc_process.has_failed():
        on_click_partial()
    root.after(1000, lambda: monitor_process_status(rpc_process, on_click_partial, root))


def main():
    ctk.set_appearance_mode('system')
    ctk.set_default_color_theme('dark-blue')
    ctk.deactivate_automatic_dpi_awareness()

    root = ctk.CTk()
    root.title('Jellyfin RPC')
    root.geometry('285x488')

    main_frame = ctk.CTkFrame(master=root)
    main_frame.pack(fill='both', expand=True)
    font = ctk.CTkFont(family='Roboto', size=14, weight='bold')

    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_path = os.path.abspath(os.path.join(bundle_dir, 'jellyfin_rpc.ini'))
    png_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    os.chdir(os.path.dirname(get_executable_path()))

    if not os.path.isfile('jellyfin_rpc.ini'):
        shutil.copyfile(ini_path, 'jellyfin_rpc.ini')
    ini_path = 'jellyfin_rpc.ini'
    config = jellyfin_rpc.get_config(ini_path)

    label1 = ctk.CTkLabel(master=main_frame, text='Checking for Update...', cursor='hand2')
    label1.bind(
        '<Button-1>', lambda _: callback('https://github.com/kennethsible/jellyfin-rpc/releases')
    )
    label1.pack(pady=(10, 0), padx=10)
    on_maximize(label1)

    scroll_frame = ctk.CTkScrollableFrame(master=main_frame, fg_color=main_frame.cget('fg_color'))
    scroll_frame.pack(fill='both', expand=True)

    entry1_text = ctk.StringVar(value=config.get('JELLYFIN_HOST'))
    entry1 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry1_text if entry1_text.get() else None,
        placeholder_text='Jellyfin Host',
        width=265,
    )
    entry1.pack(pady=(0, 5), padx=10)

    entry2_text = ctk.StringVar(value=config.get('JELLYFIN_API_KEY'))
    entry2 = ctk.CTkEntry(
        master=scroll_frame,
        textvariable=entry2_text if entry2_text.get() else None,
        placeholder_text='Jellyfin API Key',
        width=265,
    )
    entry2.pack(pady=5, padx=10)

    entry3_text = ctk.StringVar(value=config.get('JELLYFIN_USERNAME'))
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

    logger.setLevel(config['LOG_LEVEL'])
    file_hdlr = logging.FileHandler('jellyfin_rpc.log', encoding='utf-8')
    file_hdlr.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(file_hdlr)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    log_queue = multiprocessing.Queue()
    logger.addHandler(handlers.QueueHandler(log_queue))
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

    checkbox5_var = ctk.IntVar(value=config.getboolean('START_MINIMIZED', True))
    checkbox5 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Start Minimized (If Connected)', variable=checkbox5_var
    )
    checkbox5.pack(anchor='w', pady=5)

    background_type = 'Dock' if platform.system() == 'Darwin' else 'Tray'
    checkbox6_var = ctk.IntVar(value=config.getboolean('MINIMIZE_ON_CLOSE', True))
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

    checkbox7_var = ctk.IntVar(value=config.getboolean('SHOW_WHEN_PAUSED', True))
    checkbox7 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Activity When Paused', variable=checkbox7_var
    )
    checkbox7.pack(anchor='w', pady=5)

    checkbox8_var = ctk.IntVar(value=config.getboolean('SHOW_SERVER_NAME', False))
    checkbox8 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Server Name in Activity', variable=checkbox8_var
    )
    checkbox8.pack(anchor='w', pady=5)

    checkbox9_var = ctk.IntVar(value=config.getboolean('SHOW_JELLYFIN_ICON', False))
    checkbox9 = ctk.CTkCheckBox(
        master=checkbox_container1, text='Show Jellyfin Icon in Activity', variable=checkbox9_var
    )
    checkbox9.pack(anchor='w', pady=5)

    rpc_process = RPCProcess(functools.partial(jellyfin_rpc.main), log_queue)
    global button1_text
    button1_text = 'Connect'

    class AppContext(TypedDict):
        button1: Optional[ctk.CTkButton]
        icon: Optional[pystray._base.Icon]

    context: AppContext = {'button1': None, 'icon': None}

    def on_click_callback():
        assert context['button1'] is not None, 'button1 is not initialized'
        on_click(
            rpc_process,
            ini_path,
            root,
            entry1,
            entry2,
            entry3,
            entry4,
            checkbox1,
            checkbox2,
            checkbox3,
            checkbox5,
            checkbox6,
            checkbox7,
            checkbox8,
            checkbox9,
            context['button1'],
            context['icon'],
        )

    if platform.system() == 'Darwin':
        icon = None
        root.createcommand('::tk::mac::ReopenApplication', lambda: on_maximize(label1, root))
    else:
        icon = pystray.Icon(
            'jellyfin-rpc',
            Image.open(png_path),
            'Jellyfin RPC',
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda _: button1_text,
                    lambda: on_click(
                        rpc_process,
                        ini_path,
                        root,
                        entry1,
                        entry2,
                        entry3,
                        entry4,
                        checkbox1,
                        checkbox2,
                        checkbox3,
                        checkbox5,
                        checkbox6,
                        checkbox7,
                        checkbox8,
                        checkbox9,
                        button1,
                    ),
                ),
                pystray.MenuItem('Maximize', lambda: on_maximize(label1, root), default=True),
                pystray.MenuItem('Quit', lambda: on_close(rpc_process, root, context['icon'])),
            ),
        )
        icon.run_detached()
    context['icon'] = icon

    button1 = ctk.CTkButton(master=main_frame, text=button1_text, command=on_click_callback)
    button1.pack(side='bottom', pady=(5, 10), padx=10)
    context['button1'] = button1
    on_click_partial = functools.partial(
        on_click,
        rpc_process,
        ini_path,
        root,
        entry1,
        entry2,
        entry3,
        entry4,
        checkbox1,
        checkbox2,
        checkbox3,
        checkbox5,
        checkbox6,
        checkbox7,
        checkbox8,
        checkbox9,
        button1,
        icon,
    )
    if (
        config.get('JELLYFIN_HOST')
        and config.get('JELLYFIN_API_KEY')
        and config.get('JELLYFIN_USERNAME')
    ):
        on_click_partial()
        if button1_text == 'Disconnect' and config.getboolean('START_MINIMIZED', True):
            root.withdraw()
    else:
        logger.info('Awaiting Configuration')
    monitor_process_status(rpc_process, on_click_partial, root)

    if platform.system() == 'Windows':
        root.iconbitmap(ico_path)
    root.resizable(False, False)
    root.mainloop()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
