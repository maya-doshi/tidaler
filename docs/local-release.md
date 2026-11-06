# Local ISRC release

Version: **0.1.8+isrc.4**. This is a local fork build, not an upstream release.

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
py -3.13 -m pip install ".\dist\tidaler-0.1.8+isrc.4-py3-none-any.whl"
tidaler isrc USSM12209515
```

## Batch integration

### Sustained sessions (0.1.8+isrc.4)

Sargam automatically uses two private CLI processes when `config-json` advertises `worker`:
`tidaler worker --role lookup` serves ISRC and title searches; `--role download` serves resolved
downloads. Each keeps one authenticated session and connection pool across batches. There is no
HTTP listener. Stdin/stdout carry JSON lines, while diagnostic output goes to stderr.

Login once with `tidaler login` before using workers. A worker loads the saved login on its first
request and refreshes an expired access token as needed. Workers never invoke interactive login
or delete credentials on API failure. Refreshed access tokens stay in worker memory; subsequent
processes can use the saved refresh token. A changed saved login invalidates in-memory search results.

Requests are spaced by at least 0.75 seconds across both workers using `worker-gate.sqlite3` in
the configuration folder. This is a conservative integration default, not a published TIDAL quota.
HTTP 429 pauses for at least 60 seconds; HTTP 403 pauses for at least 10 minutes. Longer
`Retry-After` values are honored, including HTTP dates. A 403 may indicate permissions or account
access, not just request frequency. These responses are not automatically retried inside a request.
Sargam retains unfinished songs in the queue and pauses for a manual login when authentication fails.

Successful title searches are cached for one hour in the lookup worker (up to 1,000 entries),
scoped by session, country, quality, exact title and artist. `refresh: true` bypasses both lookup caches.
No-match and failed searches are not cached. Download folder repair, current quality settings,
ISRC validation, and Sargam's saved-file/FLAC checks still apply.

One request per line, with a unique string `request_id`:

```json
{"request_id":"lookup-1","action":"isrc","codes":["TCAFX2133359"]}
{"request_id":"lookup-2","action":"search","jobs":[{"id":"song-1","title":"Song","artist":"Artist"}]}
```

The download role accepts `action: "download"`, a `jobs` array in the resolved manifest format below,
and an optional `delay`. Responses contain `request_id`, `ok`, and `result`; failures contain
`reason`, `error`, and `retry_after`. Download events contain `request_id` and `event` with each job's
ID and `running`/`done`/`error` status. EOF closes the worker. Cancelling an active request terminates
only that worker; the next request recreates it. Older CLI releases retain Sargam's subprocess flow.

### One-shot commands

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
