from importlib.metadata import version

from .jellyfin_rpc import load_config, start_discord_rpc

__version__ = version('jellyfin-rpc')
__all__ = ['load_config', 'start_discord_rpc']
