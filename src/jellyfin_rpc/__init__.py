from importlib.metadata import version

from .main import load_config, parse_iterable, start_discord_rpc

__version__ = version('jellyfin-rpc')
__all__ = ['load_config', 'parse_iterable', 'start_discord_rpc']
