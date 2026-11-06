#!/usr/bin/env python
import json
import signal
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlparse

import typer
from requests.exceptions import RequestException
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table
from tidalapi.exceptions import TidalAPIError, TooManyRequests
from tidalapi.media import Track

from tidaler import __version__
from tidaler.config import HandlingApp, Settings, Tidal
from tidaler.constants import CTX_TIDAL, FAVORITES, MediaType
from tidaler.download import Download
from tidaler.helper.cli import parse_timestamp
from tidaler.helper.isrc import normalize_isrc, tracks_by_isrc
from tidaler.helper.isrc_batch import resolve_batch, resolved_track
from tidaler.helper.path import get_format_template, path_config_base, path_file_settings
from tidaler.helper.search_batch import MAX_JOBS, search_batch
from tidaler.helper.tidal import (
    all_artist_album_ids,
    get_tidal_media_id,
    get_tidal_media_type,
    instantiate_media,
    url_ending_clean,
)
from tidaler.helper.wrapper import LoggerWrapped
from tidaler.model.cfg import HelpSettings
from tidaler.model.cfg import Settings as DownloadSettings

app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]}, add_completion=False)
app_dl_fav = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    add_completion=True,
    help="Download from a favorites collection.",
)

app.add_typer(app_dl_fav, name="dl_fav")


def version_callback(value: bool):
    """Callback to print version and exit if version flag is set.

    Args:
        value (bool): If True, prints version and exits.
    """
    if value:
        print(f"{__version__}")

        raise typer.Exit()


@app.callback()
def callback_app(
    ctx: typer.Context,
    version: Annotated[bool | None, typer.Option("--version", "-v", callback=version_callback, is_eager=True)] = None,
):
    """App callback to initialize context and handle version option.

    Args:
        ctx (typer.Context): Typer context object.
        version (bool | None, optional): Version flag. Defaults to None.
    """
    ctx.obj = {"tidal": None}


def _handle_track_or_video(
    dl: Download, ctx: typer.Context, item: str, media: object, file_template: str, idx: int, urls_pos_last: int
) -> bool:
    """Handle downloading a track or video item.

    Args:
        dl (Download): The Download instance.
        ctx (typer.Context): Typer context object.
        item (str): The URL or identifier of the item.
        media: The media object to download.
        file_template (str): The file template for saving the media.
        idx (int): The index of the item in the list.
        urls_pos_last (int): The last index in the URLs list.
    """
    settings = ctx.obj[CTX_TIDAL].settings
    download_delay: bool = bool(settings.data.download_delay and idx < urls_pos_last)

    success, _ = dl.item(
        media=media,
        file_template=file_template,
        download_delay=download_delay,
        quality_audio=settings.data.quality_audio,
        quality_video=settings.data.quality_video,
    )
    return success


def _handle_album_playlist_mix_artist(
    ctx: typer.Context,
    dl: Download,
    handling_app: HandlingApp,
    media_type: MediaType,
    media: object,
    item_id: str,
    file_template: str,
) -> bool:
    """Handle downloading albums, playlists, mixes, or artist collections.

    Args:
        ctx (typer.Context): Typer context object.
        dl (Download): The Download instance.
        handling_app (HandlingApp): The HandlingApp instance.
        media_type (MediaType): The type of media (album, playlist, mix, or artist).
        media: The media object to download.
        item_id (str): The ID of the media item.
        file_template (str): The file template for saving the media.

    Returns:
        bool: False if aborted, True otherwise.
    """
    item_ids: list[str] = []
    settings = ctx.obj[CTX_TIDAL].settings

    if media_type == MediaType.ARTIST:
        media_type = MediaType.ALBUM
        item_ids += all_artist_album_ids(media)
    else:
        item_ids.append(item_id)

    for _item_id in item_ids:
        if handling_app.event_abort.is_set():
            return False

        dl.items(
            media_id=_item_id,
            media_type=media_type,
            file_template=file_template,
            video_download=settings.data.video_download,
            download_delay=settings.data.download_delay,
            quality_audio=settings.data.quality_audio,
            quality_video=settings.data.quality_video,
        )

    return True


def _process_url(
    dl: Download,
    ctx: typer.Context,
    handling_app: HandlingApp,
    url: str,
    idx: int,
    urls_pos_last: int,
) -> bool:
    """Process a single URL or ID for download.

    Args:
        dl (Download): The Download instance.
        ctx (typer.Context): Typer context object.
        handling_app (HandlingApp): The HandlingApp instance.
        url (str): The URL or identifier to process.
        idx (int): The index of the url in the list.
        urls_pos_last (int): The last index in the URLs list.

    Returns:
        bool: False if aborted, True otherwise.
    """
    settings = ctx.obj[CTX_TIDAL].settings

    if handling_app.event_abort.is_set():
        return False

    if "http" not in url:
        print(f"It seems like you have supplied an invalid URL: {url}")
        return True

    url_clean: str = url_ending_clean(url)

    media_type = get_tidal_media_type(url_clean)
    if not isinstance(media_type, MediaType):
        print(f"Could not determine media type for: {url_clean}")
        return True

    url_clean_id = get_tidal_media_id(url_clean)
    if not isinstance(url_clean_id, str):
        print(f"Could not determine media id for: {url_clean}")
        return True

    file_template = get_format_template(media_type, settings)
    if not isinstance(file_template, str):
        print(f"Could not determine file template for: {url_clean}")
        return True

    try:
        media = instantiate_media(ctx.obj[CTX_TIDAL].session, media_type, url_clean_id)
    except Exception:
        print(f"Media not found (ID: {url_clean_id}). Maybe it is not available anymore.")
        return True

    if media_type in [MediaType.TRACK, MediaType.VIDEO]:
        _handle_track_or_video(dl, ctx, url_clean, media, file_template, idx, urls_pos_last)
    elif media_type in [MediaType.ALBUM, MediaType.PLAYLIST, MediaType.MIX, MediaType.ARTIST]:
        return _handle_album_playlist_mix_artist(ctx, dl, handling_app, media_type, media, url_clean_id, file_template)
    return True


def _download(ctx: typer.Context, urls: list[str], try_login: bool = True, resolved: Track | None = None) -> bool:
    """Invokes download function and tracks progress.

    Args:
        ctx (typer.Context): The typer context object.
        urls (list[str]): The list of URLs to download.
        try_login (bool, optional): If true, attempts to login to TIDAL. Defaults to True.
        resolved (Track | None): A verified single track, avoiding duplicate metadata requests.

    Returns:
        bool: True if ran successfully.
    """
    if try_login:
        ctx.invoke(login, ctx)

    settings: Settings = ctx.obj[CTX_TIDAL].settings
    handling_app: HandlingApp = HandlingApp()

    progress: Progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        SpinnerColumn(),
        BarColumn(),
        TaskProgressColumn(),
        refresh_per_second=20,
        auto_refresh=True,
        expand=True,
        transient=False,
    )

    progress_overall = Progress(
        TextColumn("[progress.description]{task.description}"),
        SpinnerColumn(),
        BarColumn(),
        TaskProgressColumn(),
        refresh_per_second=20,
        auto_refresh=True,
        expand=True,
        transient=False,
    )

    fn_logger = LoggerWrapped(progress.print)

    dl = Download(
        tidal_obj=ctx.obj[CTX_TIDAL],
        skip_existing=settings.data.skip_existing,
        path_base=settings.data.download_base_path,
        fn_logger=fn_logger,
        progress=progress,
        progress_overall=progress_overall,
        event_abort=handling_app.event_abort,
        event_run=handling_app.event_run,
    )

    progress_table = Table.grid()
    progress_table.add_row(progress)
    progress_table.add_row(progress_overall)
    progress_group = Group(progress_table)

    urls_pos_last = len(urls) - 1

    with Live(progress_group, refresh_per_second=20, vertical_overflow="visible"):
        try:
            for idx, item in enumerate(urls):
                if resolved is not None and len(urls) == 1:
                    if handling_app.event_abort.is_set():
                        return False
                    file_template = get_format_template(MediaType.TRACK, settings)
                    if not isinstance(file_template, str):
                        return False
                    if not _handle_track_or_video(dl, ctx, item, resolved, file_template, idx, urls_pos_last):
                        return False
                    continue
                if _process_url(dl, ctx, handling_app, item, idx, urls_pos_last) is False:
                    return False
        finally:
            progress.refresh()
            progress.stop()

    return True


@app.command(name="cfg")
def settings_management(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    editor: Annotated[
        bool, typer.Option("--editor", "-e", help="Open the settings file in your default editor.")
    ] = False,
) -> None:
    """Print or set an option, or open the settings file in an editor.

    Args:
        names (list[str] | None, optional): None (list all options), one (list the value only for this option) or two arguments (set the value for the option). Defaults to None.
        editor (bool, optional): If set, your default system editor will be opened. Defaults to False.
    """
    if editor:
        config_path: Path = Path(path_file_settings())

        if not config_path.is_file():
            config_path.write_text('{"version": "1.0.0"}')

        config_file_str = str(config_path)

        typer.launch(config_file_str)
    else:
        settings = Settings()
        d_settings = settings.data.to_dict()

        if names:
            if names[0] not in d_settings:
                print(f'Option "{names[0]}" is not valid!')
            elif len(names) == 1:
                print(f'{names[0]}: "{d_settings[names[0]]}"')
            elif len(names) > 1:
                settings.set_option(names[0], names[1])
                settings.save()
        else:
            help_settings: dict = HelpSettings().to_dict()
            table = Table(title=f"Config: {path_file_settings()}")
            table.add_column("Key", style="cyan", no_wrap=True)
            table.add_column("Value", style="magenta")
            table.add_column("Description", style="green")

            for key, value in sorted(d_settings.items()):
                table.add_row(key, str(value), help_settings[key])

            console = Console()
            console.print(table)


@app.command(name="login")
def login(ctx: typer.Context) -> bool:
    """Login to TIDAL and update context object.

    Args:
        ctx (typer.Context): Typer context object.

    Returns:
        bool: True if login was successful, False otherwise.
    """
    print("Let us check if you are already logged in... ", end="")

    settings = Settings()
    tidal = Tidal(settings)
    result = tidal.login(fn_print=print, fn_input=input)
    ctx.obj[CTX_TIDAL] = tidal

    return result


@app.command(name="logout")
def logout() -> bool:
    """Logout from TIDAL.

    Returns:
        bool: True if logout was successful, False otherwise.
    """
    settings = Settings()
    tidal = Tidal(settings)
    result = tidal.logout()

    if result:
        print("You have been successfully logged out.")

    return result


def _lookup_isrc_tracks(ctx: typer.Context, normalized: str) -> list[Track]:
    """Authenticate and resolve tracks, reporting safe errors on stderr.

    Args:
        ctx (typer.Context): CLI context to populate during login.
        normalized (str): Validated ISRC.

    Returns:
        list[Track]: Playable matching tracks.

    Raises:
        typer.Exit: If authentication or lookup fails.
    """
    try:
        # Keep stdout suitable for piping URLs or parsing JSON, including at login.
        with redirect_stdout(sys.stderr):
            logged_in: bool = ctx.invoke(login, ctx)
        if not logged_in:
            typer.echo("TIDAL login failed. Run 'tidaler login' and try again.", err=True)
            raise typer.Exit(1)
        return tracks_by_isrc(ctx.obj[CTX_TIDAL].session, normalized)
    except TooManyRequests:
        typer.echo("TIDAL rate limit reached. Wait before retrying the ISRC lookup.", err=True)
        raise typer.Exit(1) from None
    except (RequestException, TidalAPIError, KeyError, ValueError):
        # Do not echo server response bodies, URLs with credentials, or tokens.
        typer.echo("TIDAL ISRC lookup failed. Check your connection and login, then retry.", err=True)
        raise typer.Exit(1) from None


def _isrc_record(track: Track, isrc: str) -> dict[str, object]:
    """Return JSON-safe metadata for a matching track.

    Args:
        track (Track): Matching TIDAL track.
        isrc (str): Normalized ISRC.

    Returns:
        dict[str, object]: Public metadata and canonical track URL.
    """
    return {
        "id": track.id,
        "isrc": isrc,
        "title": track.name,
        "artists": [artist.name for artist in track.artists or []],
        "album": track.album.name if track.album else None,
        "url": f"https://tidal.com/browse/track/{track.id}",
    }


def _download_isrc_match(ctx: typer.Context, tracks: list[Track]) -> None:
    """Download a unique match, refusing ambiguous editions.

    Args:
        ctx (typer.Context): Authenticated CLI context.
        tracks (list[Track]): Non-empty list of matching tracks.

    Raises:
        typer.Exit: If selection is ambiguous or downloading fails.
    """
    if len(tracks) != 1:
        typer.echo("Several tracks match. Choose one with --track-id:", err=True)
        for track in tracks:
            typer.echo(f"{track.id}: https://tidal.com/browse/track/{track.id}", err=True)
        raise typer.Exit(1)
    if not _download(ctx, [f"https://tidal.com/browse/track/{tracks[0].id}"], try_login=False):
        raise typer.Exit(1)


@app.command(
    name="isrc",
    help="Find playable TIDAL tracks by ISRC. Prints one URL per match; use --json for metadata.",
)
def lookup_isrc(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="ISRC, compact or hyphenated (for example USSM12209515).")],
    json_output: Annotated[bool, typer.Option("--json", help="Print track metadata as JSON.")] = False,
    download_match: Annotated[bool, typer.Option("--download", help="Download a matching track.")] = False,
    track_id: Annotated[int | None, typer.Option("--track-id", min=1, help="Select a matching TIDAL track ID.")] = None,
) -> None:
    """Resolve an ISRC to playable TIDAL URLs using your normal Tidaler login.

    Args:
        ctx (typer.Context): CLI context holding the authenticated session.
        code (str): ISRC to resolve.
        json_output (bool): Emit JSON instead of one URL per line.
        download_match (bool): Download the selected match.
        track_id (int | None): Explicit track selection when several editions match.

    Raises:
        typer.BadParameter: For invalid input or incompatible options.
        typer.Exit: On authentication, lookup, selection or download failure.
    """
    try:
        normalized: str = normalize_isrc(code)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="CODE") from None
    if json_output and download_match:
        message: str = "--json cannot be combined with --download."
        raise typer.BadParameter(message)

    tracks: list[Track] = _lookup_isrc_tracks(ctx, normalized)

    if track_id is not None:
        tracks = [track for track in tracks if track.id == track_id]
    if not tracks:
        if json_output:
            typer.echo("[]")
        typer.echo("No playable matching track found for this ISRC and selection.", err=True)
        raise typer.Exit(1)

    if download_match:
        _download_isrc_match(ctx, tracks)
        return

    if json_output:
        records: list[dict[str, object]] = [_isrc_record(track, normalized) for track in tracks]
        typer.echo(json.dumps(records, ensure_ascii=False))
    else:
        for track in tracks:
            typer.echo(f"https://tidal.com/browse/track/{track.id}")


@app.command(name="config-json")
def config_json() -> None:
    """Read integration capabilities and download settings in one process, without tokens."""
    with redirect_stdout(sys.stderr):
        settings = Settings()
    fields = ("download_base_path", "quality_audio", "extract_flac", "download_dolby_atmos")
    commands = ["isrc-batch", "download-resolved", "download-batch", "search-batch", "worker"]
    typer.echo(
        json.dumps({"protocol": 2, "commands": commands, **{key: getattr(settings.data, key) for key in fields}})
    )


@app.command(name="worker")
def persistent_worker(ctx: typer.Context, role: Annotated[str, typer.Option("--role")] = "lookup") -> None:
    """Serve JSON-lines on stdin/stdout with a sustained lookup or download session."""
    from tidaler.integration_worker import serve

    if role not in ("lookup", "download"):
        message = "Role must be lookup or download"
        raise typer.BadParameter(message)
    serve(role, ctx)


@app.command(name="isrc-batch")
def lookup_isrc_batch(
    ctx: typer.Context,
    codes: Annotated[list[str], typer.Argument()],
    refresh: Annotated[bool, typer.Option("--refresh", help="Ignore cached matches.")] = False,
) -> None:
    """Resolve codes in batches of 20 using one login; print a JSON map keyed by ISRC."""
    try:
        normalized = [normalize_isrc(code) for code in codes]
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    try:
        with redirect_stdout(sys.stderr):
            _integration_login(ctx)
            tidal = ctx.obj[CTX_TIDAL]
            quality = str(tidal.settings.data.quality_audio)
            matches = resolve_batch(
                tidal.session, normalized, quality, Path(path_config_base()) / "isrc-cache.sqlite3", refresh
            )
        typer.echo(
            json.dumps(
                {"matches": matches, "quality": quality, "country": tidal.session.country_code}, ensure_ascii=False
            )
        )
    except (RequestException, TidalAPIError, OSError, KeyError, ValueError, TypeError):
        typer.echo("TIDAL batch lookup failed. Check login/connection; wait before retrying if rate limited.", err=True)
        raise typer.Exit(1) from None


@app.command(name="search-batch")
def search_batch_command(
    ctx: typer.Context,
    list_file: Annotated[Path, typer.Option("--list", exists=True, dir_okay=False)],
    delay: Annotated[float, typer.Option("--delay", min=0, max=10, help="Seconds between searches.")] = 0.2,
) -> None:
    """Search a JSON list of {id, title, artist} with one login; print candidates keyed by id.

    For songs without an ISRC. Every candidate carries its ISRC, so the caller downloads its choice
    with download-resolved / download-batch exactly like an ISRC match.
    """
    try:
        jobs = json.loads(list_file.read_text(encoding="utf-8"))
        if not isinstance(jobs, list) or not jobs or len(jobs) > MAX_JOBS:
            typer.echo(f"Provide between 1 and {MAX_JOBS} songs to search.", err=True)
            raise typer.Exit(2)
        for job in jobs:
            if not isinstance(job, dict) or not str(job.get("id") or "") or not str(job.get("title") or "").strip():
                typer.echo("Each song needs an id and a title.", err=True)
                raise typer.Exit(2)
    except (OSError, TypeError, ValueError):
        typer.echo("Invalid search list.", err=True)
        raise typer.Exit(2) from None
    try:
        with redirect_stdout(sys.stderr):
            _integration_login(ctx)
            session = ctx.obj[CTX_TIDAL].session
            results = search_batch(session, jobs, delay)
        typer.echo(json.dumps({"results": results, "country": session.country_code}, ensure_ascii=False))
    except TooManyRequests:
        typer.echo("TIDAL rate limit reached. Wait before searching again.", err=True)
        raise typer.Exit(1) from None
    except (RequestException, TidalAPIError, OSError, KeyError, ValueError, TypeError):
        typer.echo("TIDAL search failed. Check login/connection, then retry.", err=True)
        raise typer.Exit(1) from None


def _integration_login(ctx: typer.Context) -> None:
    """Authenticate once and abort before lookup if login fails."""
    if not ctx.invoke(login, ctx):
        typer.echo("TIDAL login failed. Run 'tidaler login' and retry.", err=True)
        raise typer.Exit(1)


def _prepare_resolved_download(folder: Path, require_flac: bool) -> str:
    """Validate current settings and persist/verify the requested destination before download."""
    if not folder.is_absolute():
        message = "Download folder must be an absolute path"
        raise ValueError(message)
    settings = Settings()
    data = cast(DownloadSettings, settings.data)
    if Path(data.download_base_path).expanduser().resolve() != folder.resolve():
        settings.set_option("download_base_path", str(folder))
        settings.save()
        saved = json.loads(Path(path_file_settings()).read_text(encoding="utf-8"))
        if Path(saved["download_base_path"]).expanduser().resolve() != folder.resolve():
            message = "Download folder repair failed"
            raise ValueError(message)
    quality = str(data.quality_audio)
    if require_flac and (
        quality not in ("LOSSLESS", "HI_RES_LOSSLESS") or not data.extract_flac or data.download_dolby_atmos
    ):
        message = "Get in FLAC requires lossless quality, FLAC extraction, and stereo output"
        raise ValueError(message)
    return quality


@app.command(name="download-resolved")
def download_resolved(
    ctx: typer.Context,
    track_id: Annotated[int, typer.Argument(min=1)],
    isrc: Annotated[str, typer.Option("--isrc")],
    folder: Annotated[Path, typer.Option("--folder")],
    require_flac: Annotated[bool, typer.Option("--require-flac")] = False,
) -> None:
    """Download a resolved ID, verifying identity and repairing the folder in the same process."""
    try:
        code = normalize_isrc(isrc)
        quality = _prepare_resolved_download(folder, require_flac)
    except (OSError, KeyError, ValueError) as error:
        typer.echo(f"Tidaler configuration error: {error}", err=True)
        raise typer.Exit(1) from None
    try:
        _integration_login(ctx)
        track = resolved_track(
            ctx.obj[CTX_TIDAL].session, track_id, code, quality, Path(path_config_base()) / "isrc-cache.sqlite3"
        )
        if not _download(ctx, [f"https://tidal.com/browse/track/{track.id}"], try_login=False, resolved=track):
            raise typer.Exit(1)
    except (RequestException, TidalAPIError, ValueError):
        typer.echo("TIDAL resolved download failed. Check login, connection, and track availability.", err=True)
        raise typer.Exit(1) from None


def _batch_event(job_id: str, status: str, **extra: object) -> None:
    """Emit a flushed machine-readable per-song event on stdout."""
    typer.echo(json.dumps({"tidaler_event": True, "id": job_id, "status": status, **extra}), color=False)
    sys.stdout.flush()


def _batch_job(ctx: typer.Context, job: dict[str, object]) -> None:
    """Download one verified batch member; errors remain isolated to its queue entry."""
    job_id = str(job["id"])
    _batch_event(job_id, "running")
    try:
        with redirect_stdout(sys.stderr):
            quality = _prepare_resolved_download(Path(str(job["folder"])), bool(job.get("require_flac")))
    except (OSError, KeyError, ValueError):
        _batch_event(job_id, "error", error="Tidaler configuration error: check the download folder and FLAC settings.")
        return
    try:
        with redirect_stdout(sys.stderr):
            code = normalize_isrc(str(job["isrc"]))
            track = resolved_track(
                ctx.obj[CTX_TIDAL].session,
                int(str(job["track_id"])),
                code,
                quality,
                Path(path_config_base()) / "isrc-cache.sqlite3",
            )
            success = _download(ctx, [f"https://tidal.com/browse/track/{track.id}"], try_login=False, resolved=track)
        if success:
            _batch_event(job_id, "done", track_id=track.id, isrc=code)
        else:
            _batch_event(job_id, "error", error="Download did not finish")
    except (RequestException, TidalAPIError, OSError, KeyError, ValueError, typer.Exit):
        _batch_event(
            job_id, "error", error="TIDAL download failed. Check connection, settings, and track availability."
        )


@app.command(name="download-batch")
def download_batch(
    ctx: typer.Context,
    list_file: Annotated[Path, typer.Option("--list", exists=True, dir_okay=False)],
    delay: Annotated[float, typer.Option("--delay", min=0, max=600, help="Minimum seconds between song starts.")] = 0,
) -> None:
    """Download a JSON list of resolved jobs sequentially with one login and per-song JSON events."""
    try:
        jobs = json.loads(list_file.read_text(encoding="utf-8"))
        if not isinstance(jobs, list) or not jobs or len(jobs) > 20:
            typer.echo("Provide between 1 and 20 resolved jobs.", err=True)
            raise typer.Exit(2)
        for job in jobs:
            normalize_isrc(job["isrc"])
            if not str(job["track_id"]).isdigit() or not Path(job["folder"]).is_absolute() or not job.get("id"):
                typer.echo("Invalid resolved job.", err=True)
                raise typer.Exit(2)
    except (OSError, KeyError, TypeError, ValueError):
        typer.echo("Invalid resolved job list.", err=True)
        raise typer.Exit(2) from None
    try:
        with redirect_stdout(sys.stderr):
            _integration_login(ctx)
    except (RequestException, TidalAPIError, EOFError, OSError, ValueError):
        typer.echo("TIDAL batch login failed. Run 'tidaler login' and retry.", err=True)
        raise typer.Exit(1) from None
    last_start = 0.0
    for job in jobs:
        remaining = delay - (time.monotonic() - last_start)
        if remaining > 0:
            time.sleep(remaining)
        last_start = time.monotonic()
        _batch_job(ctx, job)


@app.command(name="dl")
def download(
    ctx: typer.Context,
    urls: Annotated[list[str] | None, typer.Argument()] = None,
    file_urls: Annotated[
        Path | None,
        typer.Option(
            "--list",
            "-l",
            exists=True,
            file_okay=True,
            dir_okay=False,
            writable=False,
            readable=True,
            resolve_path=True,
            help="File with URLs to download. One URL per line.",
        ),
    ] = None,
) -> bool:
    """Download media from provided URLs or a file containing URLs.

    Args:
        ctx (typer.Context): Typer context object.
        urls (list[str] | None, optional): List of URLs to download. Defaults to None.
        file_urls (Path | None, optional): Path to file containing URLs. Defaults to None.

    Returns:
        bool: True if download was successful, False otherwise.
    """
    if not urls:
        # Read the text file provided.
        if file_urls:
            text: str = file_urls.read_text()
            urls = text.splitlines()
        else:
            print("Provide either URLs or a file containing URLs (one per line).")

            raise typer.Abort()

    return _download(ctx, urls)


@app_dl_fav.command(
    name="tracks",
    help="Download your favorite track collection.",
)
def download_fav_tracks(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            "-s",
            help="Download only tracks added to favorites after this timestamp (UTC). Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS or Unix timestamp.",
        ),
    ] = None,
) -> bool:
    """Download your favorite track collection.

    Args:
        ctx (typer.Context): Typer context object.
        since (str | None, optional): Timestamp filter (UTC) for incremental downloads. Defaults to None.

    Returns:
        bool: Download result.
    """
    # Method name
    func_name_favorites: str = FAVORITES["fav_tracks"]["function_name"]

    # Parse timestamp if provided
    since_datetime: datetime | None = None
    if since:
        since_datetime = parse_timestamp(since)

    return _download_fav_factory(ctx, func_name_favorites, since_datetime)


@app_dl_fav.command(
    name="artists",
    help="Download your favorite artist collection.",
)
def download_fav_artists(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            "-s",
            help="Download only artists added to favorites after this timestamp (UTC). Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS or Unix timestamp.",
        ),
    ] = None,
) -> bool:
    """Download your favorite artist collection.

    Args:
        ctx (typer.Context): Typer context object.
        since (str | None, optional): Timestamp filter (UTC) for incremental downloads. Defaults to None.

    Returns:
        bool: Download result.
    """
    # Method name
    func_name_favorites: str = FAVORITES["fav_artists"]["function_name"]

    # Parse timestamp if provided
    since_datetime: datetime | None = None
    if since:
        since_datetime = parse_timestamp(since)

    return _download_fav_factory(ctx, func_name_favorites, since_datetime)


@app_dl_fav.command(
    name="albums",
    help="Download your favorite album collection.",
)
def download_fav_albums(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            "-s",
            help="Download only albums added to favorites after this timestamp (UTC). Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS or Unix timestamp.",
        ),
    ] = None,
) -> bool:
    """Download your favorite album collection.

    Args:
        ctx (typer.Context): Typer context object.
        since (str | None, optional): Timestamp filter (UTC) for incremental downloads. Defaults to None.

    Returns:
        bool: Download result.
    """
    # Method name
    func_name_favorites: str = FAVORITES["fav_albums"]["function_name"]

    # Parse timestamp if provided
    since_datetime: datetime | None = None
    if since:
        since_datetime = parse_timestamp(since)

    return _download_fav_factory(ctx, func_name_favorites, since_datetime)


@app_dl_fav.command(
    name="videos",
    help="Download your favorite video collection.",
)
def download_fav_videos(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            "-s",
            help="Download only videos added to favorites after this timestamp (UTC). Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS or Unix timestamp.",
        ),
    ] = None,
) -> bool:
    """Download your favorite video collection.

    Args:
        ctx (typer.Context): Typer context object.
        since (str | None, optional): Timestamp filter (UTC) for incremental downloads. Defaults to None.

    Returns:
        bool: Download result.
    """
    # Method name
    func_name_favorites: str = FAVORITES["fav_videos"]["function_name"]

    # Parse timestamp if provided
    since_datetime: datetime | None = None
    if since:
        since_datetime = parse_timestamp(since)

    return _download_fav_factory(ctx, func_name_favorites, since_datetime)


def _download_fav_factory(ctx: typer.Context, func_name_favorites: str, since: datetime | None = None) -> bool:
    """Factory which helps to download items from the favorites collections.

    Args:
        ctx (typer.Context): Typer context object.
        func_name_favorites (str): Method name to call from `tidalapi` favorites object.
        since (datetime | None, optional): Only include items added after this timestamp. Defaults to None.

    Returns:
        bool: Download result.
    """
    ctx.invoke(login, ctx)
    func_favorites: Callable = getattr(ctx.obj[CTX_TIDAL].session.user.favorites, func_name_favorites)

    # Get all favorite items
    all_media: list = list(func_favorites())

    # Filter by timestamp if provided (only for items with user_date_added attribute)
    if since is not None:
        console: Console = Console()
        console.print(f"[cyan]Filtering favorites added since: {since.strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")

        filtered_media: list = []
        for media in all_media:
            # Check if media has user_date_added attribute and it's after the since timestamp
            if hasattr(media, "user_date_added") and media.user_date_added is not None:
                if media.user_date_added >= since:
                    filtered_media.append(media)
            else:
                # If no timestamp available, include the item (conservative approach)
                filtered_media.append(media)

        console.print(f"[cyan]Found {len(all_media)} total favorites, {len(filtered_media)} match filter[/cyan]")
        media_urls: list[str] = [media.share_url for media in filtered_media]
    else:
        media_urls: list[str] = [media.share_url for media in all_media]

    return _download(ctx, media_urls, try_login=False)


@app.command()
def gui(ctx: typer.Context):
    """Launch the GUI for the application.

    Args:
        ctx (typer.Context): Typer context object.
    """
    from tidaler.gui import gui_activate

    ctx.invoke(login, ctx)
    gui_activate(ctx.obj[CTX_TIDAL])


def handle_sigint_term(signum, frame):
    """Set app abort event, so threads can check it and shutdown.

    Args:
        signum: Signal number.
        frame: Current stack frame.
    """
    handling_app: HandlingApp = HandlingApp()

    handling_app.event_abort.set()


if __name__ == "__main__":
    # Catch CTRL+C
    signal.signal(signal.SIGINT, handle_sigint_term)
    signal.signal(signal.SIGTERM, handle_sigint_term)

    # Check if the first argument is a URL. Hacky solution, since Typer does not support positional arguments without options / commands.
    if len(sys.argv) > 1:
        first_arg = sys.argv[1]
        parsed_url = urlparse(first_arg)

        if parsed_url.scheme in ["http", "https"] and parsed_url.netloc:
            # Rewrite sys.argv to simulate `dl <URL>`
            sys.argv.insert(1, "dl")

    app()
