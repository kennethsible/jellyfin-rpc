from configparser import ConfigParser
from multiprocessing import Process

import customtkinter

import jellyfin_rpc


def on_connect(
    process: Process,
    entry1: customtkinter.CTkEntry,
    entry2: customtkinter.CTkEntry,
    entry3: customtkinter.CTkEntry,
    button: customtkinter.CTkButton,
):
    config = ConfigParser()
    config.read('jellyfin_rpc.ini')
    config.set('DEFAULT', 'JELLYFIN_HOST', entry1.get())
    config.set('DEFAULT', 'API_TOKEN', entry2.get())
    config.set('DEFAULT', 'USERNAME', entry3.get())
    with open('jellyfin_rpc.ini', 'w') as ini_file:
        config.write(ini_file)
    button.configure(text='Connected', state='disabled')
    button.update()
    process.start()


def on_closing(process: Process):
    try:
        process.terminate()
        process.join()
    except AttributeError:
        pass
    exit(0)


def main():
    customtkinter.set_appearance_mode('system')
    customtkinter.set_default_color_theme('dark-blue')

    root = customtkinter.CTk()
    root.title('Jellyfin Discord RPC')

    frame = customtkinter.CTkFrame(master=root)
    frame.pack(pady=20, padx=60, fill='both', expand=True)

    config = ConfigParser()
    config.read('jellyfin_rpc.ini')

    if config['DEFAULT']['JELLYFIN_HOST']:
        entry1_text = customtkinter.StringVar()
        entry1 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry1_text,
            width=265,
        )
        entry1_text.set(config['DEFAULT']['JELLYFIN_HOST'])
    else:
        entry1 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='Jellyfin Host',
            width=265,
        )
    entry1.pack(pady=12, padx=10)

    if config['DEFAULT']['API_TOKEN']:
        entry2_text = customtkinter.StringVar()
        entry2 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry2_text,
            width=265,
        )
        entry2_text.set(config['DEFAULT']['API_TOKEN'])
    else:
        entry2 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='API Token',
            width=265,
        )
    entry2.pack(pady=12, padx=10)

    if config['DEFAULT']['USERNAME']:
        entry3_text = customtkinter.StringVar()
        entry3 = customtkinter.CTkEntry(
            master=frame,
            textvariable=entry3_text,
            width=265,
        )
        entry3_text.set(config['DEFAULT']['USERNAME'])
    else:
        entry3 = customtkinter.CTkEntry(
            master=frame,
            placeholder_text='Username',
            width=265,
        )
    entry3.pack(pady=12, padx=10)

    process = Process(target=jellyfin_rpc.main)
    button = customtkinter.CTkButton(
        master=frame,
        text='Connect to Jellyfin',
        command=lambda: on_connect(process, entry1, entry2, entry3, button),
    )
    button.pack(pady=12, padx=10)
    if (
        config['DEFAULT']['JELLYFIN_HOST']
        and config['DEFAULT']['API_TOKEN']
        and config['DEFAULT']['USERNAME']
    ):
        on_connect(process, entry1, entry2, entry3, button)

    root.protocol('WM_DELETE_WINDOW', lambda: on_closing(process))
    root.mainloop()


if __name__ == '__main__':
    main()
