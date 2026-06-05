from importlib.metadata import version

from .main import start_discord_rpc

__version__ = version('jellyfin-rpc')
__all__ = ['start_discord_rpc']
