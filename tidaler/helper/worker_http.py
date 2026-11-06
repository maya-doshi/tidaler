"""Cross-process pacing and cooldown for the two persistent integration sessions."""

import math
import sqlite3
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests


class WorkerError(requests.RequestException):
    """A safe integration error which must not be mistaken for a missing song."""

    def __init__(self, reason: str, message: str, retry_after: float = 60) -> None:
        """Store a public reason and the minimum delay before another attempt."""
        super().__init__(message)
        self.reason: str = reason
        self.retry_after: float = retry_after


class RequestGate:
    """Coordinate request starts and rejection cooldowns through a tiny SQLite file."""

    def __init__(self, path: Path, interval: float = 0.75) -> None:
        """Use one gate for all integration workers in this configuration directory."""
        self.path: Path = path
        self.interval: float = interval
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS gate (id INTEGER PRIMARY KEY, next REAL, until REAL, reason TEXT)")
            conn.execute("INSERT OR IGNORE INTO gate VALUES (1, 0, 0, '')")

    def check(self) -> None:
        """Reject locally during cooldown, without contacting TIDAL or attempting login."""
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            until, reason = conn.execute("SELECT until, reason FROM gate WHERE id=1").fetchone()
        if until > time.time():
            raise WorkerError(
                reason, "TIDAL requests are paused after an API rejection.", math.ceil(until - time.time())
            )

    def wait(self) -> None:
        """Reserve one spaced request start, then recheck for a rejection from the other worker."""
        self.check()
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            next_at = conn.execute("SELECT next FROM gate WHERE id=1").fetchone()[0]
            start = max(time.time(), next_at)
            conn.execute("UPDATE gate SET next=? WHERE id=1", (start + self.interval,))
        time.sleep(max(0, start - time.time()))
        self.check()

    def block(self, status: int, retry_after: str = "") -> WorkerError:
        """Honor Retry-After; use conservative defaults for rate limits and forbidden responses."""
        delay: float = 600 if status == 403 else 60
        try:
            supplied = float(retry_after)
        except ValueError:
            try:
                supplied = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                supplied = 0
        if math.isfinite(supplied):
            delay = max(delay, supplied)
        reason: str = "forbidden" if status == 403 else "rate_limited"
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("UPDATE gate SET until=MAX(until, ?), reason=? WHERE id=1", (time.time() + delay, reason))
        message: str = (
            "TIDAL returned 403 Forbidden. Requests paused; check account access if it persists."
            if status == 403
            else "TIDAL rate limit reached. Requests paused before retrying."
        )
        return WorkerError(reason, message, delay)


class WorkerHTTP(requests.Session):
    """Keep connections open, pace API calls, and stop on 403/429 without automatic retries."""

    persistent: bool = True

    def __init__(self, gate: RequestGate) -> None:
        """Bind this session to a gate shared with the other worker."""
        super().__init__()
        self.gate: RequestGate = gate
        self.unauthorized: bool = False

    def request(self, method: str, url: str | bytes, *args: Any, **kwargs: Any) -> requests.Response:
        """Bound network time and surface safe rejection errors before the API library consumes them."""
        self.gate.wait()
        kwargs.setdefault("timeout", 45)
        response: requests.Response = super().request(method, url, *args, **kwargs)
        if response.status_code == 401:
            if self.unauthorized:
                response.close()
                raise WorkerError(
                    "auth_required", "TIDAL authentication failed after refresh. Run 'tidaler login'.", 600
                )
            self.unauthorized = True
        if response.status_code in (403, 429):
            try:
                error = self.gate.block(response.status_code, response.headers.get("Retry-After", ""))
            finally:
                response.close()
            raise error
        return response
