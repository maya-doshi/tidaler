"""Batched title/artist search for songs without an ISRC, using one login.

Search is fuzzy: this returns every playable candidate with the metadata needed to decide (title, version, artists,
album, duration, ISRC, quality tier). The caller chooses, and downloads the choice by ID and its own ISRC, so the
downloaded file can still be verified.
"""

import re
import time
from typing import Any

from tidalapi.media import Track
from tidalapi.session import Session

from tidaler.helper.isrc import _LookupHTTP
from tidaler.helper.isrc_batch import track_record

MAX_JOBS = 50
SEARCH_LIMIT = 10
_BRACKETS = re.compile(r"\s*[(\[][^)\]]*[)\]]")


def queries(title: str, artist: str) -> list[str]:
    """Return the full title with its artist, then the title without bracketed parts.

    TIDAL keeps "(Taylor's Version)", "(Remix)" and the like in a separate version field, so a query with the
    bracket text can miss the track.
    """
    full: str = f"{title} {artist}".strip()
    base: str = _BRACKETS.sub("", title).strip()
    short: str = f"{base} {artist}".strip() if base else ""
    return list(dict.fromkeys(query for query in (full, short) if query))


def search_record(track: Track) -> dict[str, Any]:
    """Serialize a candidate: the ISRC batch record plus version and duration."""
    record: dict[str, Any] = track_record(track)
    record.update(version=track.version or "", duration=int(track.duration or 0))
    return record


def search_tracks(session: Session, title: str, artist: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Playable tracks for a title and artist, from the first query that finds any.

    Tracks without a valid ISRC are left out: the caller verifies the downloaded file by ISRC.
    """
    for query in queries(title, artist):
        result: Any = session.search(query, models=[Track], limit=limit)
        tracks: list[Track] = list(result.get("tracks") or []) if isinstance(result, dict) else []
        found: dict[int, dict[str, Any]] = {}
        for track in tracks:
            if track.id is None or not track.available or not track.isrc or track.id in found:
                continue
            try:
                found[track.id] = search_record(track)
            except ValueError:  # a malformed ISRC in the catalogue
                continue
        if found:
            return list(found.values())
    return []


def search_batch(session: Session, jobs: list[dict[str, Any]], delay: float = 0.2) -> dict[str, list[dict[str, Any]]]:
    """Search each job ({id, title, artist}) with one reused transport, a little apart.

    API, authentication and rate-limit errors propagate: a failed search is never reported as "no match".
    """
    results: dict[str, list[dict[str, Any]]] = {}
    original_http = session.request_session
    with _LookupHTTP() as http:
        session.request_session = http
        try:
            for index, job in enumerate(jobs):
                if index and delay:
                    time.sleep(delay)
                results[str(job["id"])] = search_tracks(session, str(job["title"]), str(job.get("artist") or ""))
        finally:
            session.request_session = original_http
    return results
