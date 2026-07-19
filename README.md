<!-- markdownlint-disable MD033 MD041 -->
# Discord RPC for Jellyfin

Jellyfin RPC updates your Discord status with what you're watching or listening to on your Jellyfin server. Make sure your Discord client is open and that your [Activity Privacy](https://support.discord.com/hc/en-us/articles/7931156448919-Activity-Sharing-on-Discord-FAQ) settings are configured correctly.

<p display="flex" align="center">
  <img src="images/jellyfin_rpc_series.png" alt="jellyfin_rpc_series" width="300" />
  <img src="images/jellyfin_rpc_music.png" alt="jellyfin_rpc_movie" width="300" />
</p>

## Installation

- For Windows, macOS, and Linux, download the latest release from GitHub ([see here](https://github.com/kennethsible/jellyfin-rpc/releases)).
- Alternatively, use [pip](https://www.google.com/url?sa=t&source=web&rct=j&opi=89978449&url=https://pip.pypa.io/en/stable/installation/&ved=2ahUKEwitg4Hr2fuTAxWQkIkEHchVE1gQFnoECCYQAQ&usg=AOvVaw31Hu8kE5Z4dpEnAanOzEpL) to install the CLI tool and refer to the [CLI usage](#cli-usage) section.

    ```bash
    pip install git+https://github.com/kennethsible/jellyfin-rpc.git
    ```

> [!NOTE]
> On Linux, you can use [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher) to automatically create a desktop shortcut and place Jellyfin RPC into your system's application launcher.

## Configuration

The Jellyfin host can be either a public or a local URL for your server. However, with a local URL, posters and album covers won't be retrievable from your Jellyfin server. In that case, you will need to rely on public metadata providers (see below for details). After entering your Jellyfin host, click "Connect" and use [Quick Connect](https://jellyfin.org/docs/general/server/quick-connect/) to authenticate with a user access token. To generate an API key instead of Quick Connect, go to the server dashboard and select "API Keys" under "Advanced."

If you prefer to use the CLI over the GUI (or you're on Linux), fill out the included [INI config](https://github.com/kennethsible/jellyfin-rpc/blob/main/jellyfin_rpc.ini). If you run into any issues, please change `log_level` in the INI to `DEBUG` and include the output in your GitHub Issue.

- `%AppData%\Jellyfin RPC` (Windows)
- `~/Library/Application Support/Jellyfin RPC` (macOS)
- `~/.config/Jellyfin RPC` (Linux)

> [!IMPORTANT]
> [TMDB](https://www.themoviedb.org/) can **optionally** be used to fetch posters for movies and TV shows. However, you must create a [TMDB account](https://www.themoviedb.org/signup/) and generate an [API key](https://developer.themoviedb.org/docs/getting-started). [MusicBrainz](https://musicbrainz.org/) and the [Cover Art Archive](https://coverartarchive.org/) can be used to fetch album covers.

- `jellyfin_host` is the base URL of your Jellyfin server, including the scheme and port (e.g. `http://192.168.1.100:8096`).
- `jellyfin_api_key` is the API key used to authenticate with your Jellyfin server. Leave blank to use Quick Connect instead.
- `jellyfin_username` is the Jellyfin username whose activity should be tracked. Leave blank to use Quick Connect instead.
- `filter_mode` controls whether `filter_libraries` uses a whitelist (allowed) or blacklist (blocked).
- `filter_libraries` is a comma-separated list of Jellyfin libraries to either whitelist or blacklist.
- `tmdb_api_key` is your API key for The Movie Database, used to fetch movie and show posters. Leave blank to disable TMDB lookups.
- `poster_languages` is a comma-separated list of two-letter language codes ([ISO 639-1](https://en.wikipedia.org/wiki/ISO_639-1)) for TMDB.
- `textless_posters` controls whether textless TMDB posters are prioritized over language posters.
- `season_over_series` controls whether season posters are preferred over series posters for shows.
- `release_over_group` controls whether release album covers are preferred over group album covers. The distinction between [release](https://musicbrainz.org/doc/Release) and [release group](https://musicbrainz.org/doc/Release_Group) is described in the MusicBrainz documentation. In short, a release is a specific *release* of an album that belongs to a *release group* (one per album).
- `always_use_tmdb` controls whether TMDB is the default source for posters or a fallback provider.
- `always_use_musicbrainz` controls whether MusicBrainz (via the Cover Art Archive) is the default source for album covers or a fallback provider.
- `media_types` is a comma-separated list of media types to track activity for. Valid values are `Movies`, `Shows`, and `Music`.
- `show_when_paused` shows the activity with a paused timer instead of a progress bar. If disabled, the activity stops displaying when you pause your media.
- `show_server_name` shows your server name as the activity name instead of saying 'Jellyfin'.
- `show_jellyfin_logo` shows a small Jellyfin icon in the bottom right of the poster or album cover.
- `polling_rate` is the interval, in seconds, at which Jellyfin RPC checks for playback changes.
- `seek_threshold` is the minimum change in playback position, in seconds, required to be treated as a seek rather than normal playback.
- `log_level` controls the verbosity of log messages shown in the GUI and printed to the console. Valid values are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`.
- `file_hdlr_level` controls the verbosity of log messages written to the log file, independently of `log_level`.
- `log_max_bytes` is the maximum size, in bytes, a log file is allowed to reach before it is rotated.
- `log_max_files` is the number of rotated log files to keep before the oldest is deleted.
- `singleton_port` is the local port used to detect whether Jellyfin RPC is already running, so that opening it again focuses the existing window instead of starting a second instance. Change this only if the default port conflicts with another application on your system.

## GUI Screenshot

![jellyfin_rpc_gui](images/jellyfin_rpc_gui.png)

## CLI Usage

```bash
usage: main.py [-h] --ini-path INI_PATH [--log-path LOG_PATH]

options:
  --ini-path INI_PATH
  --log-path LOG_PATH
```

### Local Build Instructions

> [!NOTE]
> For Linux builds, consult the GitHub Actions workflow for PyInstaller ([see here](https://github.com/kennethsible/jellyfin-rpc/blob/main/.github/workflows/pyinstaller.yaml)). You should use the system Python installation, as uv does not currently include font support ([astral-sh/uv/issues/15668](https://github.com/astral-sh/uv/issues/15668)).

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

2. Create Python Environment

    ```bash
    uv venv .venv --python 3.12
    ```

3. Build Standalone Executable

    ```bash
    uv run --extra gui pyinstaller main.spec
    ```
