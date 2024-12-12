import configparser
import functools
import multiprocessing
import os
import shutil
import sys
import webbrowser
from typing import Callable

import customtkinter
import pystray
import requests
from PIL import Image

import jellyfin_rpc

__version__ = '1.3.0'


class RPCProcess:

    def __init__(self, target: Callable):
        self.target = target
        self.process: multiprocessing.Process | None = None

    def start(self):
        self.process = multiprocessing.Process(target=self.target)
        self.process.start()

    def stop(self):
        if self.process is None:
            return
        if self.process.is_alive():
            self.process.terminate()
            self.process.join()


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


def on_maximize(root: customtkinter.CTk, label: customtkinter.CTkLabel):
    response = requests.get(
        'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest'
    )
    latest_ver = response.json()['tag_name'].lstrip('v')
    if latest_ver == __version__:
        label.configure(
            text_color='gray',
            text=f'Current Version ({__version__})',
        )
    else:
        label.configure(
            text_color='red',
            text=f'Update Available ({__version__} \u2192 {latest_ver})',
        )
    root.after(0, root.deiconify)


def on_close(
    rpc_process: RPCProcess,
    icon: pystray._base.Icon,
    root: customtkinter.CTk,
):
    rpc_process.stop()
    icon.visible = False
    icon.stop()
    root.quit()


def callback(url: str):
    webbrowser.open_new_tab(url)


def main():
    customtkinter.set_appearance_mode('system')
    customtkinter.set_default_color_theme('dark-blue')

    root = customtkinter.CTk()
    root.title('Jellyfin RPC')

    frame = customtkinter.CTkFrame(master=root)
    frame.pack(fill='both', expand=True)

    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_path = os.path.abspath(os.path.join(bundle_dir, 'jellyfin_rpc.ini'))
    png_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))
    if not os.path.isfile('jellyfin_rpc.ini'):
        shutil.copyfile(ini_path, 'jellyfin_rpc.ini')
    ini_path = 'jellyfin_rpc.ini'
    config = jellyfin_rpc.get_config(ini_path)

    label1 = customtkinter.CTkLabel(master=frame, cursor='hand2')
    label1.bind(
        '<Button-1>', lambda _: callback('https://github.com/kennethsible/jellyfin-rpc/releases')
    )
    label1.pack(pady=0, padx=10)

    if config['JELLYFIN_HOST']:
        entry1_text = customtkinter.StringVar()
        entry1 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry1_text,
            width=265,
        )
        entry1_text.set(config['JELLYFIN_HOST'])
    else:
        entry1 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='Jellyfin Host',
            width=265,
        )
    entry1.pack(pady=(0, 5), padx=10)

    if config['API_TOKEN']:
        entry2_text = customtkinter.StringVar()
        entry2 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry2_text,
            width=265,
        )
        entry2_text.set(config['API_TOKEN'])
    else:
        entry2 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='API Token',
            width=265,
        )
    entry2.pack(pady=5, padx=10)

    if config['USERNAME']:
        entry3_text = customtkinter.StringVar()
        entry3 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry3_text,
            width=265,
        )
        entry3_text.set(config['USERNAME'])
    else:
        entry3 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='Username',
            width=265,
        )
    entry3.pack(pady=5, padx=10)

    if config['TMDB_API_KEY']:
        entry4_text = customtkinter.StringVar()
        entry4 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry4_text,
            width=265,
        )
        entry4_text.set(config['TMDB_API_KEY'])
    else:
        entry4 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='TMDB API Key (Optional)',
            width=265,
        )
    entry4.pack(pady=5, padx=10)

    media_types = config['MEDIA_TYPES'].split(',')
    checkbox1_var = customtkinter.IntVar(value=int('Movies' in media_types))
    checkbox1 = customtkinter.CTkCheckBox(master=frame, text='Movies', variable=checkbox1_var)
    checkbox1.pack(pady=5, padx=10)

    checkbox2_var = customtkinter.IntVar(value=int('Shows' in media_types))
    checkbox2 = customtkinter.CTkCheckBox(master=frame, text='Shows', variable=checkbox2_var)
    checkbox2.pack(pady=5, padx=10)

    checkbox3_var = customtkinter.IntVar(value=int('Music' in media_types))
    checkbox3 = customtkinter.CTkCheckBox(master=frame, text='Music', variable=checkbox3_var)
    checkbox3.pack(pady=5, padx=10)

    rpc_process = RPCProcess(functools.partial(jellyfin_rpc.main))
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
    if config['JELLYFIN_HOST'] and config['API_TOKEN'] and config['USERNAME']:
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

    root.withdraw()
    icon = pystray.Icon(
        'jellyfin-rpc',
        Image.open(png_path),
        'Jellyfin RPC',
        menu=pystray.Menu(
            pystray.MenuItem('Maximize', lambda: on_maximize(root, label1), default=True),
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
