import configparser
import functools
import multiprocessing
import os
import shutil
import sys
import webbrowser
import winreg
from logging import LogRecord
from multiprocessing.queues import Queue
from typing import Callable

import customtkinter
import pystray
import requests
from PIL import Image

import jellyfin_rpc

__version__ = '1.4.3'


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


class RPCLogger:

    def __init__(
        self,
        frame: customtkinter.CTkFrame,
        log_queue: multiprocessing.Queue,
        text_widget: customtkinter.CTkTextbox,
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
        self.text_widget.insert(customtkinter.END, message)
        self.text_widget.see(customtkinter.END)

    def format_log_record(self, record: LogRecord):
        return f'{record.levelname}: {record.getMessage()}\n'


def on_click(
    rpc_process: RPCProcess,
    ini_path: str,
    entry1: customtkinter.CTkEntry,
    entry2: customtkinter.CTkEntry,
    entry3: customtkinter.CTkEntry,
    entry4: customtkinter.CTkEntry,
    checkbox1: customtkinter.CTkCheckBox,
    checkbox2: customtkinter.CTkCheckBox,
    checkbox3: customtkinter.CTkCheckBox,
    button1: customtkinter.CTkButton,
):
    if button1._text == 'Connect':
        config = configparser.ConfigParser()
        config.read(ini_path)
        config.set('DEFAULT', 'JELLYFIN_HOST', entry1.get())
        config.set('DEFAULT', 'API_TOKEN', entry2.get())
        config.set('DEFAULT', 'USERNAME', entry3.get())
        config.set('DEFAULT', 'TMDB_API_KEY', entry4.get())
        media_types = []
        if checkbox1._variable.get():
            media_types.append('Movies')
        if checkbox2._variable.get():
            media_types.append('Shows')
        if checkbox3._variable.get():
            media_types.append('Music')
        config.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))
        if not config.get('DEFAULT', 'LOG_LEVEL', fallback=None):
            config.set('DEFAULT', 'LOG_LEVEL', 'INFO')
        with open(ini_path, 'w') as ini_file:
            config.write(ini_file)
        rpc_process.start()
        for entry in (entry1, entry2, entry3, entry4, checkbox1, checkbox2, checkbox3):
            entry.configure(state='readonly')
            entry.update()
        button1.configure(text='Disconnect')
    else:
        rpc_process.stop()
        for entry in (entry1, entry2, entry3, entry4, checkbox1, checkbox2, checkbox3):
            entry.configure(state='normal')
            entry.update()
        button1.configure(text='Connect')
    button1.update()


def on_maximize(label: customtkinter.CTkLabel, root: customtkinter.CTk | None = None):
    response = requests.get(
        'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest'
    )
    release = response.json()['tag_name'].lstrip('v')
    if __version__ == release:
        label.configure(
            text=f'Current Version ({__version__})',
            text_color='gray',
        )
    else:
        label.configure(
            text=f'Update Available ({__version__} \u2192 {release})',
            text_color='cyan',
        )
    if root is not None:
        root.after(0, root.deiconify)


def on_close(rpc_process: RPCProcess, icon: pystray._base.Icon, root: customtkinter.CTk):
    rpc_process.stop()
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


def set_startup_status(enabled: bool):
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r'Software\Microsoft\Windows\CurrentVersion\Run',
        0,
        winreg.KEY_SET_VALUE,
    )
    if enabled:
        exe_path = get_executable_path()
        winreg.SetValueEx(key, 'Jellyfin RPC', 0, winreg.REG_SZ, exe_path)
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


def main():
    customtkinter.set_appearance_mode('system')
    customtkinter.set_default_color_theme('dark-blue')
    customtkinter.deactivate_automatic_dpi_awareness()

    root = customtkinter.CTk()
    root.title('Jellyfin RPC')

    frame = customtkinter.CTkFrame(master=root)
    frame.pack(fill='both', expand=True)

    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_path = os.path.abspath(os.path.join(bundle_dir, 'jellyfin_rpc.ini'))
    png_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    os.chdir(os.path.dirname(get_executable_path()))
    if not os.path.isfile('jellyfin_rpc.ini'):
        shutil.copyfile(ini_path, 'jellyfin_rpc.ini')
    ini_path = 'jellyfin_rpc.ini'
    config = jellyfin_rpc.get_config(ini_path)

    label1 = customtkinter.CTkLabel(master=frame, cursor='hand2')
    label1.bind(
        '<Button-1>', lambda _: callback('https://github.com/kennethsible/jellyfin-rpc/releases')
    )
    label1.pack(pady=0, padx=10)
    on_maximize(label1)

    entry1_text = customtkinter.StringVar(value=config.get('JELLYFIN_HOST'))
    entry1 = customtkinter.CTkEntry(
        master=frame,
        textvariable=entry1_text if entry1_text.get() else None,
        placeholder_text='Jellyfin Host',
        width=265,
    )
    entry1.pack(pady=(0, 5), padx=10)

    entry2_text = customtkinter.StringVar(value=config.get('API_TOKEN'))
    entry2 = customtkinter.CTkEntry(
        master=frame,
        textvariable=entry2_text if entry2_text.get() else None,
        placeholder_text='API Token',
        width=265,
    )
    entry2.pack(pady=5, padx=10)

    entry3_text = customtkinter.StringVar(value=config.get('USERNAME'))
    entry3 = customtkinter.CTkEntry(
        master=frame,
        textvariable=entry3_text if entry3_text.get() else None,
        placeholder_text='Username',
        width=265,
    )
    entry3.pack(pady=5, padx=10)

    entry4_text = customtkinter.StringVar(value=config.get('TMDB_API_KEY'))
    entry4 = customtkinter.CTkEntry(
        master=frame,
        textvariable=entry4_text if entry4_text.get() else None,
        placeholder_text='TMDB API Key (Optional)',
        width=265,
    )
    entry4.pack(pady=5, padx=10)

    checkbox4_var = customtkinter.IntVar(value=int(get_startup_status()))
    checkbox4 = customtkinter.CTkCheckBox(
        master=frame,
        text='Run on Windows Startup',
        variable=checkbox4_var,
        command=lambda: set_startup_status(checkbox4_var.get()),
    )
    checkbox4.pack(pady=5, padx=10)

    textbox1 = customtkinter.CTkTextbox(master=frame, width=265, height=100)
    textbox1.pack(pady=5, padx=10)
    log_queue = multiprocessing.Queue()
    RPCLogger(frame, log_queue, textbox1)

    media_types = config.get('MEDIA_TYPES', 'Movies,Shows,Music').split(',')
    checkbox1_var = customtkinter.IntVar(value=int('Movies' in media_types))
    checkbox1 = customtkinter.CTkCheckBox(master=frame, text='Movies', variable=checkbox1_var)
    checkbox1.pack(pady=5, padx=10)

    checkbox2_var = customtkinter.IntVar(value=int('Shows' in media_types))
    checkbox2 = customtkinter.CTkCheckBox(master=frame, text='Shows', variable=checkbox2_var)
    checkbox2.pack(pady=5, padx=10)

    checkbox3_var = customtkinter.IntVar(value=int('Music' in media_types))
    checkbox3 = customtkinter.CTkCheckBox(master=frame, text='Music', variable=checkbox3_var)
    checkbox3.pack(pady=5, padx=10)

    rpc_process = RPCProcess(functools.partial(jellyfin_rpc.main), log_queue)
    button1 = customtkinter.CTkButton(
        master=frame,
        text='Connect',
        command=lambda: on_click(
            rpc_process,
            ini_path,
            entry1,
            entry2,
            entry3,
            entry4,
            checkbox1,
            checkbox2,
            checkbox3,
            button1,
        ),
    )
    button1.pack(pady=(5, 10), padx=10)
    if config.get('JELLYFIN_HOST') and config.get('API_TOKEN') and config.get('USERNAME'):
        on_click(
            rpc_process,
            ini_path,
            entry1,
            entry2,
            entry3,
            entry4,
            checkbox1,
            checkbox2,
            checkbox3,
            button1,
        )
        if button1._text == 'Disconnect':
            root.withdraw()

    icon = pystray.Icon(
        'jellyfin-rpc',
        Image.open(png_path),
        'Jellyfin RPC',
        menu=pystray.Menu(
            pystray.MenuItem('Maximize', lambda: on_maximize(label1, root), default=True),
            pystray.MenuItem('Quit', lambda: on_close(rpc_process, icon, root)),
        ),
    )
    icon.run_detached()

    root.iconbitmap(ico_path)
    root.protocol('WM_DELETE_WINDOW', root.withdraw)
    root.resizable(False, False)
    root.mainloop()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
