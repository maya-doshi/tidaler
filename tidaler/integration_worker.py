"""Private JSON-lines worker: one sustained login for lookup or download requests."""

import hashlib
import json
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

import requests
import typer
from tidalapi.exceptions import AuthenticationError, TidalAPIError

from tidaler.config import Settings, Tidal
from tidaler.constants import CTX_TIDAL
from tidaler.helper.isrc import normalize_isrc
from tidaler.helper.isrc_batch import resolve_batch, resolved_track
from tidaler.helper.path import path_config_base, path_file_settings, path_file_token
from tidaler.helper.search_batch import search_batch
from tidaler.helper.worker_http import RequestGate, WorkerError, WorkerHTTP
from tidaler.model.cfg import Settings as DownloadSettings
from tidaler.model.cfg import Token


def invalid(message: str) -> NoReturn:
    """Reject malformed protocol data without echoing its potentially sensitive contents."""
    raise ValueError(message)


def validate_job(job: Any, action: str, identities: set[str]) -> None:
    """Validate one search or resolved-download entry before authentication."""
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
        invalid("Each job needs a string ID")
    if job["id"] in identities:
        invalid("Duplicate job ID")
    identities.add(job["id"])
    if action == "search":
        if not isinstance(job.get("title"), str) or not job["title"].strip():
            invalid("Each search needs a title")
    else:
        normalize_isrc(job["isrc"])
        if int(job["track_id"]) < 1 or not Path(job["folder"]).is_absolute():
            invalid("Invalid track ID or absolute folder")
        if not isinstance(job.get("require_flac", False), bool):
            invalid("require_flac must be boolean")


class Worker:
    """Serve one role serially while retaining authentication, connections, and title-search cache."""

    def __init__(self, role: str, ctx: typer.Context) -> None:
        """Create local resources only; login is lazy on the first real request."""
        self.role: str = role
        self.ctx: typer.Context = ctx
        self.base: Path = Path(path_config_base())
        self.gate: RequestGate = RequestGate(self.base / "worker-gate.sqlite3")
        self.http: WorkerHTTP = WorkerHTTP(self.gate)
        self.tidal: Tidal | None = None
        self.authenticated: bool = False
        self.token_stamp: int = 0
        self.token_digest: str = ""
        self.search_cache: dict[tuple[str, str, str, str], tuple[float, list[dict[str, Any]]]] = {}

    def close(self) -> None:
        """Release the pooled HTTP connections when stdin closes."""
        self.http.close()

    def prepare(self) -> Tidal:
        """Load saved credentials once, refresh only near expiry, and reload local download preferences."""
        self.gate.check()
        token_path: Path = Path(path_file_token())
        if not token_path.is_file():
            raise WorkerError("auth_required", "Run 'tidaler login' in a terminal first.", 600)
        settings: Settings = Settings()
        settings.data = cast(Any, DownloadSettings).from_json(Path(path_file_settings()).read_text(encoding="utf-8"))
        if self.tidal is None:
            self.tidal = Tidal(settings)
            self.tidal.session.request_session.close()
            self.tidal.session.request_session = self.http
            self.ctx.obj[CTX_TIDAL] = self.tidal
        tidal: Tidal = self.tidal
        stamp: int = token_path.stat().st_mtime_ns
        if not self.authenticated or stamp != self.token_stamp:
            # Do not use login_token(): its legacy error path deletes saved credentials on API failures.
            token: Token = cast(Any, Token).from_json(token_path.read_text(encoding="utf-8"))
            if not token.token_type or not token.access_token:
                raise WorkerError("auth_required", "Run 'tidaler login' in a terminal first.", 600)
            token_expiry: datetime | None = (
                datetime.fromtimestamp(token.expiry_time, UTC) if token.expiry_time else None
            )
            digest: str = hashlib.sha256(
                json.dumps([token.token_type, token.access_token, token.refresh_token]).encode()
            ).hexdigest()
            if not self.authenticated or digest != self.token_digest:
                self.authenticated = False
                self.search_cache.clear()
                if not tidal.session.load_oauth_session(
                    token.token_type, token.access_token, token.refresh_token, token_expiry, is_pkce=True
                ):
                    raise WorkerError("auth_required", "Saved TIDAL login is invalid. Run 'tidaler login'.", 600)
                tidal.is_pkce = True
                self.authenticated = True
                self.token_digest = digest
            self.token_stamp = stamp
        expiry: datetime | None = tidal.session.expiry_time
        now: datetime = datetime.now(UTC)
        if (
            expiry
            and (expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry) <= now
            and (not tidal.session.refresh_token or not tidal.session.token_refresh(tidal.session.refresh_token))
        ):
            self.authenticated = False
            raise WorkerError("auth_required", "TIDAL session expired. Run 'tidaler login'.", 600)
        tidal.settings_apply(settings)
        return tidal

    def validate(self, request: dict[str, Any]) -> None:
        """Reject malformed jobs before making any authenticated request."""
        action: Any = request.get("action")
        if action not in ({"isrc", "search"} if self.role == "lookup" else {"download"}):
            invalid("Unsupported operation for this worker role")
        if action == "isrc":
            codes: Any = request.get("codes")
            if not isinstance(codes, list) or not 1 <= len(codes) <= 1000:
                invalid("Provide 1 to 1000 ISRCs")
            for code in codes:
                normalize_isrc(code)
            return
        jobs: Any = request.get("jobs")
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= (50 if action == "search" else 20):
            invalid("Invalid job count")
        identities: set[str] = set()
        for job in jobs:
            validate_job(job, action, identities)
        delay: float = float(request.get("delay", 0))
        if not 0 <= delay <= 600:
            invalid("Delay must be between 0 and 600 seconds")

    def handle(self, request: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Dispatch one request with the existing session and safe per-song download events."""
        self.validate(request)
        self.http.unauthorized = False
        tidal: Tidal = self.prepare()
        action: str = request["action"]
        quality: str = str(cast(DownloadSettings, tidal.settings.data).quality_audio)
        if action == "isrc":
            return {
                "matches": resolve_batch(
                    tidal.session,
                    request["codes"],
                    quality,
                    self.base / "isrc-cache.sqlite3",
                    bool(request.get("refresh")),
                )
            }
        if action == "search":
            results: dict[str, list[dict[str, Any]]] = {}
            for job in request["jobs"]:
                key: tuple[str, str, str, str] = (
                    str(tidal.session.country_code),
                    quality,
                    job["title"],
                    str(job.get("artist") or ""),
                )
                cached = self.search_cache.get(key)
                if cached and cached[0] > time.time() and not request.get("refresh"):
                    results[job["id"]] = cached[1]
                    continue
                found = search_batch(tidal.session, [job], delay=0)[job["id"]]
                results[job["id"]] = found
                if found:  # Never retain transient failures or negative catalogue results.
                    if len(self.search_cache) >= 1000:
                        self.search_cache.pop(next(iter(self.search_cache)))
                    self.search_cache[key] = (time.time() + 3600, found)
            return {"results": results}
        self.download(request, emit)
        return {}

    def download(self, request: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
        """Download sequentially; API rejection aborts the batch, leaving remaining jobs retryable."""
        from tidaler.cli import _download, _prepare_resolved_download

        last_start: float = 0
        for job in request["jobs"]:
            time.sleep(max(0, float(request.get("delay", 0)) - (time.monotonic() - last_start)))
            last_start = time.monotonic()
            emit({"id": job["id"], "status": "running", "stage": "validating", "tidaler_event": True})
            tidal: Tidal = self.prepare()
            try:
                quality = _prepare_resolved_download(Path(job["folder"]), job.get("require_flac", False))
            except (OSError, ValueError, KeyError):
                emit(
                    {
                        "id": job["id"],
                        "status": "error",
                        "tidaler_event": True,
                        "error": "Tidaler configuration error: check the folder and FLAC settings.",
                    }
                )
                continue
            try:
                track = resolved_track(
                    tidal.session, int(job["track_id"]), job["isrc"], quality, self.base / "isrc-cache.sqlite3"
                )
                completed_files: list[str] = []
                emit({"id": job["id"], "status": "running", "stage": "downloading", "tidaler_event": True})
                success = _download(
                    self.ctx,
                    [f"https://tidal.com/browse/track/{track.id}"],
                    try_login=False,
                    resolved=track,
                    completed_files=completed_files,
                )
                self.gate.check()  # Download may catch an HTTP exception internally.
                emit(
                    {
                        "id": job["id"],
                        "status": "done" if success else "error",
                        "tidaler_event": True,
                        "error": "" if success else "Download did not finish",
                        "files": completed_files,
                    }
                )
            except ValueError:
                emit(
                    {
                        "id": job["id"],
                        "status": "error",
                        "tidaler_event": True,
                        "error": "Resolved track is no longer available with this ISRC.",
                    }
                )


def serve(role: str, ctx: typer.Context, incoming: TextIO | None = None, outgoing: TextIO | None = None) -> None:
    """Read private stdin requests until EOF, separating protocol stdout from SDK output."""
    source: TextIO = incoming or sys.stdin
    target: TextIO = outgoing or sys.stdout

    def send(payload: dict[str, Any]) -> None:
        """Flush one bounded, credential-free response or event."""
        target.write(json.dumps(payload, ensure_ascii=False) + "\n")
        target.flush()

    with redirect_stdout(sys.stderr):
        worker: Worker = Worker(role, ctx)
    try:
        while line := source.readline(1_000_001):
            identity: str = ""
            try:
                if len(line) > 1_000_000:
                    return  # Do not parse the remainder of an oversized request as another request.
                request: Any = json.loads(line)
                if not isinstance(request, dict) or not isinstance(request.get("request_id"), str):
                    invalid("A request ID is required")
                identity = request["request_id"]
                with redirect_stdout(sys.stderr):
                    result = worker.handle(
                        request, lambda event, identity=identity: send({"request_id": identity, "event": event})
                    )
                send({"request_id": identity, "ok": True, "result": result})
            except WorkerError as error:
                send(
                    {
                        "request_id": identity,
                        "ok": False,
                        "reason": error.reason,
                        "error": str(error),
                        "retry_after": error.retry_after,
                    }
                )
            except AuthenticationError:
                worker.authenticated = False
                send(
                    {
                        "request_id": identity,
                        "ok": False,
                        "reason": "auth_required",
                        "error": "TIDAL authentication failed. Run 'tidaler login'.",
                        "retry_after": 600,
                    }
                )
            except (requests.RequestException, TidalAPIError):
                auth_failed = worker.http.unauthorized
                if auth_failed:
                    worker.authenticated = False
                send(
                    {
                        "request_id": identity,
                        "ok": False,
                        "reason": "auth_required" if auth_failed else "temporary",
                        "error": (
                            "TIDAL authentication failed. Run 'tidaler login'."
                            if auth_failed
                            else "TIDAL request failed; retry later."
                        ),
                        "retry_after": 600 if auth_failed else 60,
                    }
                )
            except (ValueError, KeyError, TypeError, OSError):
                send(
                    {
                        "request_id": identity,
                        "ok": False,
                        "reason": "invalid_request",
                        "error": "Check the worker request and Tidaler configuration.",
                        "retry_after": 60,
                    }
                )
    finally:
        worker.close()
