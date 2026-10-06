import functools
import html
import logging
import multiprocessing as mp
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from configparser import ConfigParser, SectionProxy
from dataclasses import dataclass
from json.decoder import JSONDecodeError
from logging import LogRecord, handlers
from multiprocessing.queues import Queue
from types import FrameType

import certifi
import requests
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QIcon, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from requests.exceptions import RequestException

from jellyfin_rpc import __version__, start_discord_rpc
from jellyfin_rpc.main import (
    build_auth_header,
    get_device_id,
    get_valid_level,
    load_config,
    parse_delimited_list,
)

SINGLE_INSTANCE_PORT = 57634
logger = logging.getLogger('GUI')
logging.addLevelName(15, 'VERBOSE')


@dataclass
class LabeledEntry:
    widget: QLineEdit
    obfuscate: bool


def get_executable_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    else:
        return os.path.abspath(__file__)


def set_startup_status(enabled: bool) -> None:
    exe_path = get_executable_path()

    if sys.platform == 'win32':
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, 'Jellyfin RPC', 0, winreg.REG_SZ, f'"{exe_path}"')
            else:
                try:
                    winreg.DeleteValue(key, 'Jellyfin RPC')
                except FileNotFoundError:
                    pass

    elif sys.platform == 'linux':
        autostart_dir = os.path.expanduser('~/.config/autostart')
        desktop_file = os.path.join(autostart_dir, 'jellyfin-rpc.desktop')
        if enabled:
            os.makedirs(autostart_dir, exist_ok=True)
            exe_dir = os.path.dirname(exe_path)
            icon_path = os.path.join(exe_dir, 'jellyfin-rpc.png')
            if not os.path.exists(icon_path):
                icon_path = 'jellyfin-rpc'
            desktop_entry = (
                '[Desktop Entry]\n'
                'Type=Application\n'
                f'Version={__version__}\n'
                'Name=Jellyfin RPC\n'
                'Comment=Discord Rich Presence for Jellyfin\n'
                f'Exec="{exe_path}"\n'
                f'Icon={icon_path}\n'
                'Terminal=false\n'
                'StartupNotify=false\n'
            )
            with open(desktop_file, 'w', encoding='utf-8') as f:
                f.write(desktop_entry)
        else:
            if os.path.exists(desktop_file):
                os.remove(desktop_file)


def get_startup_status() -> bool:
    if sys.platform == 'win32':
        import winreg

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

    elif sys.platform == 'linux':
        desktop_file = os.path.expanduser('~/.config/autostart/jellyfin-rpc.desktop')
        return os.path.exists(desktop_file)

    return False


def open_file(filepath: str) -> None:
    if sys.platform == 'win32':
        os.startfile(filepath)
    elif sys.platform == 'darwin':
        subprocess.call(('open', filepath))
    elif sys.platform == 'linux':
        subprocess.call(('xdg-open', filepath))


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


def setup_ipc_server(gui_queue: queue.Queue[str], singleton_port: int) -> socket.socket:
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 TCP
        server.bind(('127.0.0.1', singleton_port))
        server.listen(5)
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(('127.0.0.1', singleton_port))
                client.sendall(b'FOCUS\n')
                if client.recv(16).strip() == b'ACK':
                    sys.exit(0)
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass
        logger.error(f'Singleton Port {singleton_port} Already In Use')
        sys.exit(1)

    def listen_for_focus() -> None:
        while True:
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(1.0)
                    if connection.recv(1024).strip() == b'FOCUS':
                        gui_queue.put('FOCUS')
                        connection.sendall(b'ACK\n')
            except OSError:
                break

    threading.Thread(target=listen_for_focus, daemon=True).start()
    return server


class RPCProcess:
    def __init__(
        self, target: Callable[[Queue[LogRecord]], None], log_queue: Queue[LogRecord]
    ) -> None:
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
        self.process = None

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


class UpdateChecker(QThread):
    update_signal = Signal(str)

    @staticmethod
    def parse_version(version_tag: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version_tag.lstrip('v').split('.'))

    def run(self) -> None:
        try:
            response = requests.get(
                'https://api.github.com/repos/kennethsible/jellyfin-rpc/releases/latest',
                timeout=5,
                verify=certifi.where(),
            )
            response.raise_for_status()
            version_tag = response.json()['tag_name'].lstrip('v')
            if self.parse_version(__version__) < self.parse_version(version_tag):
                self.update_signal.emit(version_tag)
        except (RequestException, JSONDecodeError, KeyError) as e:
            logger.warning(f'GitHub Version Check Failed ({type(e).__name__})')
            logger.debug(e)


class SegmentedButton(QWidget):
    def __init__(self, options: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.button_group = QButtonGroup(self)
        self.button_group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}

        for i, text in enumerate(options):
            button = QPushButton(text)
            button.setFixedHeight(28)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if i == 0:
                button.setStyleSheet('border-top-right-radius: 0; border-bottom-right-radius: 0;')
            else:
                button.setStyleSheet('border-top-left-radius: 0; border-bottom-left-radius: 0;')

            self.button_group.addButton(button, i)
            layout.addWidget(button)
            self.buttons[text] = button

    def set_value(self, value: str) -> None:
        if value in self.buttons:
            self.buttons[value].setChecked(True)

    def get_value(self) -> str:
        button = self.button_group.checkedButton()
        return button.text() if button else ''


class LibrarySelectorWindow(QDialog):
    def __init__(
        self,
        parent: QWidget,
        config: SectionProxy,
        jf_host: str,
        jf_api_key: str,
        jf_username: str,
        library_filter_type: str,
        selected_libraries: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Library Selector')
        self.resize(200, 300)
        self.setModal(True)

        self.jf_host = jf_host
        self.jf_api_key = jf_api_key
        self.jf_username = jf_username
        self.selected_libraries = selected_libraries
        self.checkbox_map: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        title = QLabel(library_filter_type)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet('font-weight: bold; font-size: 14px; margin-bottom: 8px;')
        layout.addWidget(title)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self.scroll_content)
        layout.addWidget(self.scroll_area)

        self.button_save = QPushButton('Save Selection')
        self.button_save.setFixedHeight(28)
        self.button_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button_save.clicked.connect(self.save_selection)
        layout.addWidget(self.button_save)

        self.retrieve_libraries(config)

    def retrieve_libraries(self, config: SectionProxy) -> None:
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
                self.scroll_layout.addWidget(QLabel(f'User Not Found: {self.jf_username}'))
                return

            response = requests.get(
                f'{self.jf_host}/Users/{user_id}/Views',
                headers=headers,
                timeout=5,
                verify=certifi.where(),
            )
            response.raise_for_status()
            if not (libraries := response.json().get('Items', [])):
                self.scroll_layout.addWidget(QLabel('No Libraries Found'))
                return

            selected_list = [x.strip() for x in self.selected_libraries.split(',') if x.strip()]
            for library in libraries:
                library_id = library.get('Id')
                checkbox = QCheckBox(library.get('Name'))
                checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
                checkbox.setChecked(library_id in selected_list)
                self.scroll_layout.addWidget(checkbox)
                self.checkbox_map[library_id] = checkbox

        except RequestException as e:
            logger.error(f'Failed to Retrieve Libraries: {e}')
            self.scroll_layout.addWidget(QLabel('Error Retrieving Libraries'))

    def save_selection(self):
        self.selected_libraries = ','.join(
            [
                library_id
                for library_id, checkbox in self.checkbox_map.items()
                if checkbox.isChecked()
            ]
        )
        self.accept()


class JellyfinRPCWindow(QWidget):
    def __init__(
        self,
        ini_path: str,
        log_path: str,
        config: SectionProxy,
        gui_queue: queue.Queue[str],
        log_queue: Queue[LogRecord],
        png_bundle_path: str,
    ) -> None:
        super().__init__()
        self.ipc_server: socket.socket | None = None

        self.ini_path = ini_path
        self.log_path = log_path
        self.config = config
        self.gui_queue = gui_queue
        self.log_queue = log_queue
        self.png_bundle_path = png_bundle_path

        self.rpc_process = RPCProcess(
            functools.partial(start_discord_rpc, ini_path, log_path), log_queue
        )
        self.is_connected = False

        self.entries: dict[str, LabeledEntry] = {}
        self.checkboxes: dict[str, QCheckBox] = {}
        self.advanced_vars: dict[str, QLineEdit] = {}

        self.create_window()
        self.load_config()
        self.setup_tray()

        self.gui_timer = QTimer(self)
        self.gui_timer.timeout.connect(self.poll_gui_queue)
        self.gui_timer.start(100)

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.poll_log_queue)
        self.log_timer.start(100)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.poll_process_status)
        self.status_timer.start(1000)

        self.update_checker = UpdateChecker(self)
        self.update_checker.update_signal.connect(self.show_update_banner)
        self.update_checker.start()

        if self.entries['JELLYFIN_HOST'].widget.text():
            self.toggle_connection()
            if not self.checkboxes['START_MINIMIZED'].isChecked():
                self.show()
        else:
            self.show()
            logger.info('Enter Host and Click Connect')

    def create_window(self):
        self.setWindowTitle(f'Jellyfin RPC v{__version__}')
        self.setMinimumSize(850, 560)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(25, 10, 25, 18)

        self.label_update = QLabel('')
        self.label_update.setOpenExternalLinks(True)
        self.label_update.setStyleSheet('font-weight: bold; font-size: 14px;')
        self.label_update.hide()
        main_layout.addWidget(self.label_update, alignment=Qt.AlignmentFlag.AlignCenter)

        grid_layout = QHBoxLayout()
        grid_layout.setSpacing(35)
        main_layout.addLayout(grid_layout, stretch=1)

        col1 = QVBoxLayout()
        col1.setSpacing(8)
        col2 = QVBoxLayout()
        col2.setSpacing(8)
        col3 = QVBoxLayout()
        col3.setSpacing(8)

        grid_layout.addLayout(col1, stretch=1)
        grid_layout.addLayout(col2, stretch=1)
        grid_layout.addLayout(col3, stretch=1)

        col1.addWidget(self.create_header('Jellyfin Settings'))
        self.entries['JELLYFIN_HOST'] = self.create_labeled_entry(col1, 'Jellyfin Host')
        self.entries['JELLYFIN_API_KEY'] = self.create_labeled_entry(
            col1, 'Jellyfin API Key', 'Leave Blank for Quick Connect', obfuscate=True
        )
        self.entries['JELLYFIN_USERNAME'] = self.create_labeled_entry(
            col1, 'Jellyfin Username', 'Leave Blank for Quick Connect'
        )

        library_layout = QVBoxLayout()
        library_layout.setSpacing(6)

        self.segmented_filter_type = SegmentedButton(['Denylist', 'Allowlist'])
        self.segmented_filter_type.button_group.buttonClicked.connect(self.on_setting_changed)
        library_layout.addWidget(self.segmented_filter_type)

        button_select_libraries = QPushButton('Select Libraries')
        button_select_libraries.setFixedHeight(28)
        button_select_libraries.setCursor(Qt.CursorShape.PointingHandCursor)
        button_select_libraries.clicked.connect(self.select_libraries)
        library_layout.addWidget(button_select_libraries)

        col1.addLayout(library_layout)

        self.text_log = QTextBrowser()
        self.text_log.setOpenExternalLinks(True)
        col1.addWidget(self.text_log, stretch=1)

        col2.addWidget(self.create_header('Poster Settings'))
        self.entries['TMDB_API_KEY'] = self.create_labeled_entry(
            col2, 'TMDB API Key', 'Leave Blank to Disable', obfuscate=True
        )
        self.entries['POSTER_LANGUAGES'] = self.create_labeled_entry(
            col2, 'Poster Language(s)', 'Leave Blank to Disable'
        )

        self.create_checkbox(col2, 'ALWAYS_USE_TMDB', 'Always Use The Movie Database')
        self.create_checkbox(col2, 'SEASON_OVER_SERIES', 'Prefer Season Poster Over Series')
        self.create_checkbox(col2, 'TEXTLESS_POSTERS', 'Prefer Textless TMDB Posters')

        col2.addWidget(self.create_header('Album Cover Settings'))
        self.create_checkbox(col2, 'ALWAYS_USE_MUSICBRAINZ', 'Always Use The Cover Art Archive')
        self.create_checkbox(col2, 'RELEASE_OVER_GROUP', 'Prefer Release Cover Over Group')

        col2.addWidget(self.create_header('Media Settings'))
        self.create_checkbox(col2, 'MOVIES', 'Show Watching Activity for Movies')
        self.create_checkbox(col2, 'SHOWS', 'Show Watching Activity for Shows')
        self.create_checkbox(col2, 'MUSIC', 'Show Listening Activity for Music')
        col2.addStretch()

        col3.addWidget(self.create_header('Activity Settings'))
        self.create_checkbox(col3, 'SHOW_WHEN_PAUSED', 'Show Activity While Paused')
        self.create_checkbox(col3, 'SHOW_JELLYFIN_LOGO', 'Show Jellyfin Logo (Small Image)')
        self.create_checkbox(col3, 'SHOW_SERVER_NAME', 'Show Jellyfin Server Name')
        self.create_checkbox(col3, 'IMDB_EXTERNAL_URLS', 'Prefer IMDb for External URLs')

        col3.addWidget(self.create_header('System Settings'))
        if sys.platform in ('win32', 'linux'):  # TODO
            checkbox_startup = QCheckBox('Open Jellyfin RPC on Startup')
            checkbox_startup.setCursor(Qt.CursorShape.PointingHandCursor)
            checkbox_startup.setChecked(get_startup_status())
            checkbox_startup.toggled.connect(set_startup_status)
            col3.addWidget(checkbox_startup)

        self.create_checkbox(col3, 'START_MINIMIZED', 'Start Minimized (If Connected)')
        self.create_checkbox(col3, 'MINIMIZE_ON_CLOSE', 'Close Button Minimizes to Tray')

        col3.addWidget(self.create_header('Advanced Settings'))

        advanced_wrapper = QHBoxLayout()
        advanced_wrapper.addStretch()

        advanced_layout = QGridLayout()
        advanced_layout.setHorizontalSpacing(8)
        advanced_layout.setVerticalSpacing(8)
        advanced_wrapper.addLayout(advanced_layout)

        advanced_wrapper.addStretch()
        col3.addLayout(advanced_wrapper)

        self.advanced_vars['POLLING_RATE'] = self.create_spinbox_row(
            advanced_layout, 0, 'Polling Rate'
        )
        self.advanced_vars['SEEK_THRESHOLD'] = self.create_spinbox_row(
            advanced_layout, 1, 'Seek Threshold'
        )

        label_log_level = QLabel('Console Log Level')
        label_log_level.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        advanced_layout.addWidget(label_log_level, 2, 0)

        self.checkbox_log_level = QComboBox()
        self.checkbox_log_level.setFixedHeight(28)
        self.checkbox_log_level.setCursor(Qt.CursorShape.PointingHandCursor)
        self.checkbox_log_level.addItems(
            ['DEBUG', 'VERBOSE', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        )
        self.checkbox_log_level.currentTextChanged.connect(self.on_setting_changed)
        advanced_layout.addWidget(self.checkbox_log_level, 2, 1, 1, 3)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button_open_ini = QPushButton('Open INI')
        button_open_ini.setFixedSize(100, 28)
        button_open_ini.setCursor(Qt.CursorShape.PointingHandCursor)
        button_open_ini.clicked.connect(lambda: open_file(self.ini_path))

        button_open_log = QPushButton('Open Log')
        button_open_log.setFixedSize(100, 28)
        button_open_log.setCursor(Qt.CursorShape.PointingHandCursor)
        button_open_log.clicked.connect(lambda: open_file(self.log_path))

        button_layout.addWidget(button_open_ini)
        button_layout.addWidget(button_open_log)

        col3.addSpacing(6)
        col3.addLayout(button_layout)
        col3.addStretch()

        main_layout.addSpacing(6)
        self.button_connect = QPushButton('Connect')
        self.button_connect.setFixedSize(130, 28)
        self.button_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button_connect.clicked.connect(self.toggle_connection)
        main_layout.addWidget(self.button_connect, alignment=Qt.AlignmentFlag.AlignCenter)

    def create_header(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            'font-weight: bold; font-size: 15px; margin-top: 10px; margin-bottom: 4px;'
        )
        return label

    def create_labeled_entry(
        self, layout: QVBoxLayout, label_text: str, placeholder: str = '', obfuscate: bool = False
    ) -> LabeledEntry:
        container = QVBoxLayout()
        container.setSpacing(2)
        container.setContentsMargins(0, 0, 0, 0)
        container.addWidget(QLabel(label_text))

        entry = QLineEdit()
        entry.setFixedHeight(28)
        entry.setPlaceholderText(placeholder)
        if obfuscate:
            entry.setEchoMode(QLineEdit.EchoMode.Password)
        container.addWidget(entry)

        layout.addLayout(container)
        return LabeledEntry(entry, obfuscate)

    def create_checkbox(self, layout: QVBoxLayout, key: str, text: str) -> QCheckBox:
        checkbox = QCheckBox(text)
        checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        checkbox.clicked.connect(self.on_setting_changed)
        layout.addWidget(checkbox)
        self.checkboxes[key] = checkbox
        return checkbox

    def create_spinbox_row(self, layout: QGridLayout, row: int, text: str) -> QLineEdit:
        layout.addWidget(QLabel(text), row, 0, Qt.AlignmentFlag.AlignRight)

        button_dec = QPushButton('-')
        button_dec.setObjectName('spin_button')
        button_dec.setFixedSize(28, 28)
        button_dec.setCursor(Qt.CursorShape.PointingHandCursor)

        entry = QLineEdit()
        entry.setFixedSize(50, 28)
        entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        entry.setReadOnly(True)

        button_inc = QPushButton('+')
        button_inc.setObjectName('spin_button')
        button_inc.setFixedSize(28, 28)
        button_inc.setCursor(Qt.CursorShape.PointingHandCursor)

        def adjust(offset: int) -> None:
            old_value = int(entry.text().rstrip('s'))
            new_value = max(1, old_value + offset)
            entry.setText(f'{new_value}s')
            self.on_setting_changed()

        button_dec.clicked.connect(lambda: adjust(-1))
        button_inc.clicked.connect(lambda: adjust(1))

        layout.addWidget(button_dec, row, 1)
        layout.addWidget(entry, row, 2)
        layout.addWidget(button_inc, row, 3)
        return entry

    def load_config(self) -> None:
        self.entries['JELLYFIN_HOST'].widget.setText(self.config.get('JELLYFIN_HOST', ''))
        self.entries['JELLYFIN_API_KEY'].widget.setText(self.config.get('JELLYFIN_API_KEY', ''))
        self.entries['JELLYFIN_USERNAME'].widget.setText(self.config.get('JELLYFIN_USERNAME', ''))
        self.entries['TMDB_API_KEY'].widget.setText(self.config.get('TMDB_API_KEY', ''))
        self.entries['POSTER_LANGUAGES'].widget.setText(self.config.get('POSTER_LANGUAGES', ''))

        library_filter_type = self.config.get('LIBRARY_FILTER_TYPE', 'DENYLIST').capitalize()
        library_filter_type = {'Blacklist': 'Denylist', 'Whitelist': 'Allowlist'}.get(
            library_filter_type, library_filter_type
        )
        self.segmented_filter_type.set_value(str(library_filter_type))
        self.selected_libraries = self.config.get('SELECTED_LIBRARIES', '')

        media_types = parse_delimited_list(self.config, 'MEDIA_TYPES')
        self.checkboxes['MOVIES'].setChecked('Movies' in media_types)
        self.checkboxes['SHOWS'].setChecked('Shows' in media_types)
        self.checkboxes['MUSIC'].setChecked('Music' in media_types)

        checkbox_vars = [
            ('SHOW_WHEN_PAUSED', True),
            ('SHOW_SERVER_NAME', False),
            ('SHOW_JELLYFIN_LOGO', True),
            ('IMDB_EXTERNAL_URLS', False),
            ('ALWAYS_USE_TMDB', False),
            ('TEXTLESS_POSTERS', False),
            ('SEASON_OVER_SERIES', False),
            ('ALWAYS_USE_MUSICBRAINZ', False),
            ('RELEASE_OVER_GROUP', False),
            ('START_MINIMIZED', True),
            ('MINIMIZE_ON_CLOSE', True),
        ]
        for key, default in checkbox_vars:
            self.checkboxes[key].setChecked(self.config.getboolean(key, default))

        self.advanced_vars['POLLING_RATE'].setText(
            f'{max(1, self.config.getint("POLLING_RATE", 5))}s'
        )
        self.advanced_vars['SEEK_THRESHOLD'].setText(
            f'{max(1, self.config.getint("SEEK_THRESHOLD", 10))}s'
        )

        log_level = self.config.get('LOG_LEVEL', 'INFO').upper()
        self.checkbox_log_level.setCurrentText(log_level)

    def save_config(self) -> None:
        config_parser = ConfigParser()
        config_parser.read(self.ini_path, encoding='utf-8')

        for key in [
            'JELLYFIN_HOST',
            'JELLYFIN_API_KEY',
            'JELLYFIN_USERNAME',
            'TMDB_API_KEY',
            'POSTER_LANGUAGES',
        ]:
            config_parser.set('DEFAULT', key, self.entries[key].widget.text())

        config_parser.set('DEFAULT', 'LIBRARY_FILTER_TYPE', self.segmented_filter_type.get_value())
        config_parser.set('DEFAULT', 'SELECTED_LIBRARIES', self.selected_libraries)
        config_parser.set(
            'DEFAULT', 'POLLING_RATE', self.advanced_vars['POLLING_RATE'].text().rstrip('s')
        )
        config_parser.set(
            'DEFAULT', 'SEEK_THRESHOLD', self.advanced_vars['SEEK_THRESHOLD'].text().rstrip('s')
        )
        config_parser.set('DEFAULT', 'LOG_LEVEL', self.checkbox_log_level.currentText())

        media_types = [
            key.capitalize()
            for key in ['MOVIES', 'SHOWS', 'MUSIC']
            if self.checkboxes[key].isChecked()
        ]
        config_parser.set('DEFAULT', 'MEDIA_TYPES', ','.join(media_types))

        for key in self.checkboxes:
            if key not in ('MOVIES', 'SHOWS', 'MUSIC'):
                config_parser.set('DEFAULT', key, str(self.checkboxes[key].isChecked()).lower())

        with open(self.ini_path, 'w', encoding='utf-8') as f:
            config_parser.write(f)

    def select_libraries(self) -> None:
        jf_host = self.entries['JELLYFIN_HOST'].widget.text().rstrip('/')
        if not jf_host:
            logger.error('Missing Jellyfin Host')
            return

        jf_api = self.entries['JELLYFIN_API_KEY'].widget.text()
        if not jf_api:
            logger.error('Missing Jellyfin API Key')
            return

        selector = LibrarySelectorWindow(
            self,
            self.config,
            jf_host,
            jf_api,
            self.entries['JELLYFIN_USERNAME'].widget.text(),
            self.segmented_filter_type.get_value(),
            self.selected_libraries,
        )
        if selector.exec():
            self.selected_libraries = selector.selected_libraries
            self.on_setting_changed()

    def on_setting_changed(self):
        if self.sender() in (
            self.checkboxes['START_MINIMIZED'],
            self.checkboxes.get('MINIMIZE_ON_CLOSE'),
        ):
            self.save_config()
            return

        self.save_config()
        if self.is_connected:
            self.toggle_connection()

        if self.sender() == self.checkbox_log_level:
            logger.setLevel(get_valid_level(self.checkbox_log_level.currentText(), logging.INFO))

    def toggle_connection(self) -> None:
        self.save_config()
        if not self.is_connected:
            self.rpc_process.start()
            for entry_data in self.entries.values():
                entry_data.widget.setReadOnly(True)
                entry_data.widget.setEnabled(False)
                if entry_data.obfuscate:
                    entry_data.widget.setEchoMode(QLineEdit.EchoMode.Password)
            self.button_connect.setText('Disconnect')
            if hasattr(self, 'tray_icon'):
                self.action_connect.setText('Disconnect')
                self.tray_icon.setToolTip('Jellyfin RPC\nConnected')
            self.is_connected = True
        else:
            self.rpc_process.stop()
            for entry_data in self.entries.values():
                entry_data.widget.setReadOnly(False)
                entry_data.widget.setEnabled(True)
                if entry_data.obfuscate:
                    entry_data.widget.setEchoMode(QLineEdit.EchoMode.Normal)
            self.button_connect.setText('Connect')
            if hasattr(self, 'tray_icon'):
                self.action_connect.setText('Connect')
                self.tray_icon.setToolTip('Jellyfin RPC\nDisconnected')
            self.is_connected = False

    def setup_tray(self) -> None:
        icon = QIcon(self.png_bundle_path)
        if icon.isNull():
            icon = self.style().standardIcon(self.style().StandardPixmap.SP_TitleBarMenuButton)

        menu = QMenu(self)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip('Jellyfin RPC\nDisconnected')

        self.action_connect = menu.addAction('Connect')
        font = self.action_connect.font()
        font.setBold(True)
        self.action_connect.setFont(font)
        self.action_connect.triggered.connect(self.toggle_connection)

        window_text = 'Hide' if self.isVisible() and not self.isMinimized() else 'Show'
        self.action_window = menu.addAction(f'{window_text} Window')
        self.action_window.triggered.connect(self.toggle_window)

        menu.addSeparator()
        action_quit = menu.addAction('Quit Jellyfin RPC')
        action_quit.triggered.connect(self.quit_window)

        if sys.platform == 'darwin':
            self.tray_menu = menu
        else:
            self.tray_icon.setContextMenu(menu)

        self.tray_icon.activated.connect(self.activate_tray)
        self.tray_icon.show()

    def activate_tray(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.maximize_window()
        elif (
            reason == QSystemTrayIcon.ActivationReason.Context
            and sys.platform == 'darwin'
            and hasattr(self, 'tray_menu')
        ):
            from PySide6.QtGui import QCursor

            self.tray_menu.exec(QCursor.pos())

    def show_update_banner(self, version_tag: str) -> None:
        releases_url = 'https://github.com/kennethsible/jellyfin-rpc/releases'
        html_content = f'Update Available ({__version__} &rarr; {version_tag})'
        html_link = f'<a href="{releases_url}" style="color: #3DAEE9; text-decoration: none;">{html_content}</a>'
        self.label_update.setText(html_link)
        self.label_update.show()

    def poll_log_queue(self) -> None:
        color_map = {
            'DEBUG': '#95A5A6',
            'VERBOSE': '#7DC2E7',
            'INFO': '#3DAEE9',
            'WARNING': '#F67400',
            'ERROR': '#DA4453',
            'CRITICAL': '#DA4453',
        }
        while not self.log_queue.empty():
            try:
                record = self.log_queue.get_nowait()
                message = html.escape(record.getMessage())
                message = re.sub(
                    r'(https?://\S+)',
                    r'<a href="\1" style="color:#1f6aa5; text-decoration:none;">\1</a>',
                    message,
                )
                html_str = f'<font color="{color_map.get(record.levelname, "#ffffff")}">{record.levelname}</font>: {message}'

                self.text_log.moveCursor(QTextCursor.MoveOperation.End)
                self.text_log.insertHtml(html_str + '<br>')
                self.text_log.moveCursor(QTextCursor.MoveOperation.End)
            except (queue.Empty, RuntimeError):
                pass

    def poll_process_status(self) -> None:
        status_text = self.text_log.toPlainText()
        if (
            not self.entries['JELLYFIN_API_KEY'].widget.text()
            and 'via Quick Connect' in status_text
        ):
            config_parser = load_config(self.ini_path)
            jf_api = config_parser.get('JELLYFIN_API_KEY', '')
            jf_user = config_parser.get('JELLYFIN_USERNAME', '')
            if jf_api and jf_user:
                self.entries['JELLYFIN_API_KEY'].widget.setText(jf_api)
                self.entries['JELLYFIN_USERNAME'].widget.setText(jf_user)

        if self.rpc_process.has_failed():
            self.toggle_connection()

    def poll_gui_queue(self) -> None:
        try:
            message = self.gui_queue.get_nowait()
            if message == 'CONNECT':
                self.toggle_connection()
            elif message in ('MAXIMIZE', 'FOCUS'):
                self.maximize_window()
            elif message == 'QUIT':
                self.quit_window()
        except queue.Empty:
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.checkboxes.get('MINIMIZE_ON_CLOSE')
            and self.checkboxes['MINIMIZE_ON_CLOSE'].isChecked()
            and event.spontaneous()
        ):
            self.action_window.setText('Show Window')
            event.ignore()
            self.hide()
        else:
            self.quit_window()

    def maximize_window(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self.action_window.setText('Hide Window')

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
            self.action_window.setText('Show Window')
        else:
            self.maximize_window()

    def quit_window(self) -> None:
        self.save_config()
        self.rpc_process.stop()
        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
        QApplication.quit()
        os._exit(0)


def apply_theme(app: QApplication) -> None:
    app.setStyle('Fusion')
    palette = QPalette()
    background_color = QColor(36, 36, 36)
    text_color = QColor(220, 228, 232)

    palette.setColor(QPalette.ColorRole.Window, background_color)
    palette.setColor(QPalette.ColorRole.WindowText, text_color)
    palette.setColor(QPalette.ColorRole.Base, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.AlternateBase, background_color)
    palette.setColor(QPalette.ColorRole.ToolTipBase, background_color)
    palette.setColor(QPalette.ColorRole.ToolTipText, text_color)
    palette.setColor(QPalette.ColorRole.Text, text_color)
    palette.setColor(QPalette.ColorRole.Button, QColor(52, 54, 56))
    palette.setColor(QPalette.ColorRole.ButtonText, text_color)
    palette.setColor(QPalette.ColorRole.Link, QColor(31, 106, 165))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(31, 106, 165))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    app.setStyleSheet("""
        QWidget { 
            font-family: "Roboto", "Segoe UI", Arial, sans-serif; 
            font-size: 13px; 
        }
        QLineEdit, QComboBox { 
            background-color: #343638; 
            border: 1px solid #565b5e; 
            border-radius: 4px; 
            padding: 4px 8px; 
            color: #dce4e8; 
        }
        QLineEdit:disabled, QComboBox:disabled { 
            background-color: #2a2d2e; 
            color: #7a8489; 
        }
        QPushButton { 
            background-color: #1f6aa5; 
            border-radius: 4px; 
            padding: 6px 12px; 
            color: white; 
            font-weight: bold; 
        }
        QPushButton:hover { 
            background-color: #144870; 
        }
        QPushButton:disabled { 
            background-color: #2a2d2e; 
            color: #7a8489; 
        }
        QPushButton#spin_button {
            padding: 0px;
            font-size: 15px;
            font-weight: bold;
        }
        SegmentedButton QPushButton {
            background-color: #2b2b2b;
            border: 1px solid #565b5e;
            color: #a2a8ab;
            font-weight: normal;
            padding: 4px 0px;
        }
        SegmentedButton QPushButton:hover:!checked {
            background-color: #343638;
            color: #ffffff;
        }
        SegmentedButton QPushButton:checked {
            background-color: #1f6aa5;
            border: 1px solid #1f6aa5;
            color: #ffffff;
            font-weight: bold;
        }
        QCheckBox { 
            spacing: 7px;
            color: #dce4e8;
        }
        QCheckBox:hover { 
            color: #ffffff; 
        }
        QCheckBox::indicator { 
            width: 20px; 
            height: 20px; 
            border-radius: 4px; 
            border: 2px solid #565b5e; 
            background-color: #343638; 
        }
        QCheckBox::indicator:hover { 
            border-color: #3daee9; 
        }
        QCheckBox::indicator:checked { 
            background-color: #1f6aa5; 
            border: 2px solid #1f6aa5; 
        }
        QCheckBox::indicator:checked:hover { 
            background-color: #2980b9; 
            border-color: #2980b9; 
        }
        QTextBrowser { 
            background-color: #2b2b2b; 
            border-radius: 4px; 
            border: 1px solid #565b5e; 
            padding: 6px; 
        }
        QScrollBar:vertical { 
            background: #242424; 
            width: 12px; 
            margin: 0px; 
        }
        QScrollBar::handle:vertical { 
            background: #565b5e; 
            min-height: 20px; 
            border-radius: 6px; 
            margin: 2px; 
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { 
            height: 0px; 
        }
    """)


def main() -> None:
    app = QApplication(sys.argv)
    apply_theme(app)

    ini_name, log_name = 'jellyfin_rpc.ini', 'jellyfin_rpc.log'
    png_name = 'icon_menubar.png' if sys.platform == 'darwin' else 'icon.png'
    bundle_dir = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    ini_bundle_path = os.path.abspath(os.path.join(bundle_dir, ini_name))
    png_bundle_path = os.path.abspath(os.path.join(bundle_dir, png_name))
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
    singleton_port = config.getint('SINGLETON_PORT', fallback=SINGLE_INSTANCE_PORT)

    gui_queue: queue.Queue[str] = queue.Queue()
    ipc_server = setup_ipc_server(gui_queue, singleton_port)

    log_level = config.get('LOG_LEVEL', 'INFO').upper()
    log_queue = setup_logging(log_level, log_path)

    rpc_window = JellyfinRPCWindow(
        ini_path, log_path, config, gui_queue, log_queue, png_bundle_path
    )
    rpc_window.ipc_server = ipc_server

    def signal_handler(signum: int, frame: FrameType | None) -> None:
        rpc_window.quit_window()

    signal.signal(signal.SIGINT, signal_handler)
    sys.exit(app.exec())


if __name__ == '__main__':
    mp.freeze_support()
    main()
