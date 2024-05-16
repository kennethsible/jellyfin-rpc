import configparser
import multiprocessing
import os
import sys
from typing import Callable

import customtkinter
import pystray
from PIL import Image

from jellyfin_rpc import get_config, set_discord_rpc


class RPCProcess:

    def __init__(self, target: Callable):
        self.target = target
        self.process: multiprocessing.Process | None = None

    def start(self):
        self.process = multiprocessing.Process(target=self.target)
        self.process.join

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
    button: customtkinter.CTkButton,
):
    if button._text == 'Connect':
        config = configparser.ConfigParser()
        config.read(ini_path)
        config.set('DEFAULT', 'JELLYFIN_HOST', entry1.get())
        config.set('DEFAULT', 'API_TOKEN', entry2.get())
        config.set('DEFAULT', 'USERNAME', entry3.get())
        config.set('DEFAULT', 'TMDB_API_KEY', entry4.get())
        with open(ini_path, 'w') as ini_file:
            config.write(ini_file)
        rpc_process.start()
        for entry in (entry1, entry2, entry3, entry4):
            entry.configure(state='readonly')
            entry.update()
        button.configure(text='Disconnect')
    else:
        rpc_process.stop()
        for entry in (entry1, entry2, entry3, entry4):
            entry.configure(state='normal')
            entry.update()
        button.configure(text='Connect')
    button.update()


def on_maximize(root: customtkinter.CTk):
    root.after(0, root.deiconify)


def on_close(
    rpc_process: RPCProcess,
    icon: pystray.Icon,
    root: customtkinter.CTk,
):
    rpc_process.stop()
    icon.visible = False
    icon.stop()
    root.quit()


def main():
    customtkinter.set_appearance_mode('system')
    customtkinter.set_default_color_theme('dark-blue')

    root = customtkinter.CTk()
    root.title('Jellyfin Discord RPC')

    frame = customtkinter.CTkFrame(master=root)
    frame.pack(pady=20, padx=60, fill='both', expand=True)

    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_path = os.path.abspath(os.path.join(bundle_dir, 'jellyfin_rpc.ini'))
    png_path = os.path.abspath(os.path.join(bundle_dir, 'icon.png'))
    ico_path = os.path.abspath(os.path.join(bundle_dir, 'icon.ico'))

    config = get_config(ini_path)

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
    entry1.pack(pady=12, padx=10)

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
    entry2.pack(pady=12, padx=10)

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
    entry3.pack(pady=12, padx=10)

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
    entry4.pack(pady=12, padx=10)

    rpc_process = RPCProcess(lambda: set_discord_rpc(ini_path))
    button = customtkinter.CTkButton(
        master=frame,
        text='Connect',
        command=lambda: on_click(rpc_process, ini_path, entry1, entry2, entry3, entry4, button),
    )
    button.pack(pady=12, padx=10)
    if config['JELLYFIN_HOST'] and config['API_TOKEN'] and config['USERNAME']:
        on_click(rpc_process, ini_path, entry1, entry2, entry3, entry4, button)

    root.withdraw()
    icon = pystray.Icon(
        'jellyfin-rpc',
        Image.open(png_path),
        'Jellyfin RPC',
        menu=pystray.Menu(
            pystray.MenuItem('Maximize', lambda: on_maximize(root), default=True),
            pystray.MenuItem('Quit', lambda: on_close(rpc_process, icon, root)),
        ),
    )
    icon.run_detached()

    root.iconbitmap(ico_path)
    root.protocol('WM_DELETE_WINDOW', root.withdraw)
    root.mainloop()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
