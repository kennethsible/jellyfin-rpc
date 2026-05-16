from importlib.metadata import version

from .main import get_media_types, load_config, start_discord_rpc

__version__ = version('jellyfin-rpc')
__all__ = ['get_media_types', 'load_config', 'start_discord_rpc']
