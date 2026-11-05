"""Batched ISRC resolution with account-scoped public metadata caching."""

import json
import logging
import sqlite3
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

from tidalapi.exceptions import ObjectNotFound
from tidalapi.media import Track
from tidalapi.session import Session

from tidaler.helper.isrc import _LookupHTTP, normalize_isrc, tracks_by_isrc

logger = logging.getLogger(__name__)
CACHE_SECONDS = 7 * 86400


def quality_rank(track: Track) -> int:
    """Return the advertised stereo quality tier, not a measured stream bitrate."""
    tags: set[str] = {str(tag) for tag in track.media_metadata_tags or []}
    quality: str = str(track.audio_quality or "")
    if "HIRES_LOSSLESS" in tags or quality == "HI_RES_LOSSLESS":
        return 3
    if "LOSSLESS" in tags or quality == "LOSSLESS":
        return 2
    return 1


def requested_rank(quality: str) -> int:
    """Map a saved quality preference to the minimum advertised tier."""
    return {"HI_RES_LOSSLESS": 3, "LOSSLESS": 2}.get(quality, 1)


def track_record(track: Track) -> dict[str, Any]:
    """Serialize public metadata only; never persist sessions or credentials."""
    return {
        "id": track.id,
        "isrc": normalize_isrc(track.isrc or ""),
        "title": track.name,
        "artists": [artist.name for artist in track.artists or []],
        "album": track.album.name if track.album else None,
        "url": f"https://tidal.com/browse/track/{track.id}",
        "quality_rank": quality_rank(track),
    }


class MatchCache:
    """SQLite cache shared safely by independent CLI processes; positive matches expire."""

    def __init__(self, path: Path, scope: str) -> None:
        """Initialize a cache path and account/country/quality scope."""
        self.path: Path = path
        self.scope: str = scope

    def read(self, code: str) -> list[dict[str, Any]] | None:
        """Read a fresh positive result; corrupt or absent caches are harmless misses."""
        if not self.path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as conn:
                row = conn.execute(
                    "SELECT payload FROM matches WHERE scope=? AND isrc=? AND expires>?",
                    (self.scope, code, time.time()),
                ).fetchone()
            records = json.loads(row[0]) if row else None
            if (
                isinstance(records, list)
                and records
                and all(
                    isinstance(record, dict) and record.get("isrc") == code and str(record.get("id", "")).isdigit()
                    for record in records
                )
            ):
                return records
        except (OSError, sqlite3.Error, ValueError):
            logger.warning("ISRC cache could not be read; using the API")
        return None

    def write(self, code: str, records: list[dict[str, Any]]) -> None:
        """Store successful matches atomically; an empty result invalidates the old entry."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS matches (scope TEXT, isrc TEXT, payload TEXT, expires REAL, PRIMARY KEY(scope,isrc))"
                )
                conn.execute(
                    "DELETE FROM matches WHERE expires<? OR (scope=? AND isrc=?)", (time.time(), self.scope, code)
                )
                if records:
                    conn.execute(
                        "INSERT INTO matches VALUES (?,?,?,?)",
                        (self.scope, code, json.dumps(records), time.time() + CACHE_SECONDS),
                    )
        except (OSError, sqlite3.Error):
            logger.warning("ISRC cache could not be written; lookup remains usable")


def match_cache(session: Session, quality: str, path: Path) -> MatchCache | None:
    """Scope cached public IDs by user, country, quality, and cache format version."""
    user_id: Any = getattr(session.user, "id", None)
    if not session.country_code or user_id is None:
        return None
    return MatchCache(path, json.dumps([1, str(user_id), session.country_code, quality]))


def _batch_candidates(session: Session, chunk: list[str]) -> dict[str, list[Track]]:
    """Read a batch and hydrate each candidate once to check its ISRC and availability."""
    with session.request.request(
        "GET", "tracks", params={"filter[isrc]": ",".join(chunk)}, base_url=session.config.openapi_v2_location
    ) as response:
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        message = "Unexpected TIDAL batch response"
        raise TypeError(message)
    candidates: dict[str, list[Track]] = {code: [] for code in chunk}
    for resource in data["data"]:
        try:
            track = session.track(str(resource["id"]))
        except ObjectNotFound:
            continue
        if track.available and track.isrc and normalize_isrc(track.isrc) in candidates:
            candidates[normalize_isrc(track.isrc)].append(track)
    return candidates


def resolve_batch(
    session: Session, values: list[str], quality: str, path: Path, refresh: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Resolve unique codes in groups of 20, reusing login and falling back for quality.

    Batch responses expose only one edition per ISRC. Hydrate it to verify availability;
    search all individual candidates when it is absent, mismatched, or below the requested tier.
    API/authentication failures propagate and are never cached as missing tracks.
    """
    codes: list[str] = list(dict.fromkeys(normalize_isrc(value) for value in values))
    cache: MatchCache | None = match_cache(session, quality, path)
    results: dict[str, list[dict[str, Any]]] = {}
    for code in codes:
        hit = cache.read(code) if cache and not refresh else None
        if hit:
            results[code] = hit
    pending: list[str] = [code for code in codes if code not in results]
    original_http = session.request_session
    with _LookupHTTP() as http:
        session.request_session = http
        try:
            for offset in range(0, len(pending), 20):
                chunk = pending[offset : offset + 20]
                candidates = _batch_candidates(session, chunk)
                for code in chunk:
                    tracks = candidates[code]
                    if not tracks or max(quality_rank(track) for track in tracks) < requested_rank(quality):
                        tracks = tracks_by_isrc(session, code, reuse_transport=True)
                    tracks.sort(key=lambda track: (-quality_rank(track), int(track.id or 0)))
                    results[code] = [track_record(track) for track in tracks]
                    if cache:
                        cache.write(code, results[code])
        finally:
            session.request_session = original_http
    return results


def resolved_track(session: Session, track_id: int, code: str, quality: str, path: Path) -> Track:
    """Validate a cached ID directly; only search again if its identity or availability changed."""
    code = normalize_isrc(code)
    cache = match_cache(session, quality, path)
    cached = cache.read(code) if cache else None
    track: Track | None = None
    with suppress(ObjectNotFound):
        track = session.track(str(track_id))
    if (
        track
        and track.available
        and track.isrc == code
        and (
            quality_rank(track) >= requested_rank(quality)
            or any(
                str(item["id"]) == str(track_id) and quality_rank(track) >= item.get("quality_rank", 3)
                for item in cached or []
            )
        )
    ):
        return track
    if cache:
        cache.write(code, [])
    matches = tracks_by_isrc(session, code)
    if not matches:
        message = "The resolved track is unavailable and no playable replacement matches its ISRC"
        raise ValueError(message)
    matches.sort(key=lambda item: (-quality_rank(item), int(item.id or 0)))
    if cache:
        cache.write(code, [track_record(item) for item in matches])
    return matches[0]
