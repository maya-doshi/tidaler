# Local ISRC release

Version: **0.1.8+isrc.3**. This is a local fork build, not an upstream release.

## Windows CLI

Extract the release ZIP, open PowerShell in that folder, and run:

```powershell
.\tidaler.exe --version
.\tidaler.exe login
.\tidaler.exe isrc USSM12209515
.\tidaler.exe isrc USSM12209515 --json
.\tidaler.exe isrc USSM12209515 --download
```

If several editions match, add `--track-id ID` to select one.
The portable executable includes Python and CLI dependencies. It does not
include the Qt GUI or FFmpeg. Configure FFmpeg separately if your downloads
require conversion. Login and settings use Tidaler's normal configuration folder.

## Python package

With Python 3.12 or 3.13, install the wheel from `dist`:

```powershell
py -3.13 -m pip install ".\dist\tidaler-0.1.8+isrc.3-py3-none-any.whl"
tidaler isrc USSM12209515
```

## Batch integration

```powershell
.\tidaler.exe isrc-batch TCAFX2133359 USSM12209515
.\tidaler.exe config-json
.\tidaler.exe download-resolved 205851680 --isrc TCAFX2133359 --folder "D:\songs\downloads" --require-flac
.\tidaler.exe download-batch --list resolved.json --delay 0
```

`isrc-batch` logs in once, sends at most 20 ISRCs per filter request, verifies candidates,
and returns JSON keyed by ISRC. A batch result below your requested quality tier falls back
to individual lookup. Positive matches are cached for seven days in `isrc-cache.sqlite3`
beside settings, scoped by account, country, and quality. `--refresh` bypasses this cache.
Failures are not cached as empty matches. Rate-limit errors stop lookup; retry later.

`download-resolved` verifies the ID's ISRC and availability directly instead of repeating
the ISRC search. An unavailable or changed ID falls back to a fresh lookup. The command
repairs/verifies the requested folder and checks FLAC settings before downloading.

`resolved.json` is an array of 1–20 jobs, for example:

```json
[{"id":"job-1","track_id":205851680,"isrc":"TCAFX2133359","folder":"D:\\songs\\downloads","require_flac":true}]
```

`download-batch` authenticates once and downloads those jobs sequentially. Stdout contains
JSON-line `running`, `done`, and `error` events carrying the job ID; progress goes to stderr.
A failed song does not prevent later songs from downloading. `--delay` sets the minimum
interval between song starts. Sargam also verifies saved files before marking a job done.

## Search for songs without an ISRC

```powershell
.	idaler.exe search-batch --list songs.json --delay 0.2
```

`songs.json` is an array of 1–50 songs, `[{"id":"job-1","title":"Delicate","artist":"Taylor Swift"}]`.
It logs in once and searches each title with its artist, then (if that finds nothing) the title without
bracketed parts, since TIDAL keeps "(Taylor's Version)" or "(Live)" in a separate version field. It prints
one JSON document keyed by song id: every playable candidate with its ID, ISRC, title, `version`, artists,
album, `duration` (seconds) and `quality_rank`. Choosing the right candidate is up to the caller; the chosen
ID and its ISRC go to `download-resolved` / `download-batch` like any ISRC match, so the file is still
verified by ISRC. A rate limit or API error exits 1 and is never reported as "no match".
`config-json` lists the supported integration commands under `commands`.

## Rebuild

From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . pytest ruff black pyright build pyinstaller
.\.venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File scripts\build-local.ps1
```

The build script uses the project-local environment, builds a wheel and sdist,
freezes the CLI with PyInstaller, checks the executable's version/help, and
packages a ZIP with the license and these instructions. SHA-256 hashes and
the resolved dependency versions are written to `dist`. No publishing, Git
tags, or remote repository updates are performed.

The automated tests mock authentication and API responses. A live authenticated
lookup must be checked with your own account; the local build does not include
credentials.
