# Discord RPC for Jellyfin

Jellyfin RPC updates your Discord status with what you're watching or listening to on your Jellyfin server. Make sure your Discord client is open while using Jellyfin RPC.

![jellyfin_rpc_series](images/jellyfin_rpc_series.png)
![jellyfin_rpc_movie](images/jellyfin_rpc_music.png)

## Installation

- Download [Latest Release](https://github.com/kennethsible/jellyfin-rpc/releases)
- Build from Source
   1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
   2. Install Python<br>`uv install python`
   3. Create Python Environment<br>`uv sync --extra gui`
   4. Build Standalone Executable<br>`uv run pyinstaller main.spec`

## Configuration

To generate a Jellyfin API key, go to the server dashboard and select **API Keys** under **Advanced**.

- Jellyfin Host (e.g., <https://jellyfin.example.com>)
- Jellyfin API Key
- Jellyfin Username
- TMDB API Key (Optional)

If you prefer to use the CLI over the GUI (or you're on Linux), fill out the included [INI config](https://github.com/kennethsible/jellyfin-rpc/blob/main/jellyfin_rpc.ini). If you run into any issues, please change `log_level` in the INI to `DEBUG` and include the output in your GitHub Issue.

- `%AppData%\Jellyfin RPC`
- `~/Library/Application Support/Jellyfin RPC`

To fetch posters and album covers, your media must be properly tagged with the appropriate provider IDs.

> [!IMPORTANT]
> [**TMDB**](https://www.themoviedb.org/) is used to fetch posters for movies and TV shows. However, you must create a [TMDB account](https://www.themoviedb.org/signup/) and generate an [API key](https://developer.themoviedb.org/docs/getting-started). [**MusicBrainz**](https://musicbrainz.org/) and the [**Cover Art Archive**](https://coverartarchive.org/) are used to fetch album covers.

## Usage (GUI)

![jellyfin_rpc_gui](images/jellyfin_rpc_gui.png)
![jellyfin_rpc_gui_2](images/jellyfin_rpc_gui_2.png)

![jellyfin_rpc_ico](images/jellyfin_rpc_ico.png)

## Usage (CLI)

To install the CLI script, run the command `pip install .` inside the cloned repository.

```bash
jellyfin-rpc [-h] --ini-path INI_PATH [--log-path LOG_PATH]
```
