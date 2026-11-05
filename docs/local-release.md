# Local ISRC release

Version: **0.1.8+isrc.1**. This is a local fork build, not an upstream release.

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
py -3.13 -m pip install ".\dist\tidaler-0.1.8+isrc.1-py3-none-any.whl"
tidaler isrc USSM12209515
```

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
