# Discord RPC for Jellyfin

![jellyfin_rpc_series](images/jellyfin_rpc_series.png)
![jellyfin_rpc_movie](images/jellyfin_rpc_music.png)

## Installation

- Download [Latest Release](https://github.com/kennethsible/jellyfin-rpc/releases) (**Recommended**)
- Build from Source (for Development)
   1. Install [Python](https://www.python.org/downloads/) and [uv](https://docs.astral.sh/uv/getting-started/installation/)
   2. Create Python Environment<br>`uv sync --extra gui`
   3. Build Executable<br>`uv run pyinstaller main.spec`

## Configuration

Use GUI or `jellyfin_rpc.ini`

- Jellyfin Host
- API Token
- Username
- TMDB API Key (Optional)

> [!IMPORTANT]
> You need a [TMDB API key](https://developer.themoviedb.org/docs/getting-started) to fetch posters for movies and series.

## Usage (GUI)

![jellyfin_rpc_gui](images/jellyfin_rpc_gui.png)

> [!NOTE]
> The Jellyfin RPC GUI opens **minimized** in the system tray.

![jellyfin_rpc_ico](images/jellyfin_rpc_ico.png)

## Usage (CLI)

```bash
jellyfin_rpc.py [-h] [--ini-path INI_PATH] [--log-path LOG_PATH] [--refresh-rate REFRESH_RATE]
```
