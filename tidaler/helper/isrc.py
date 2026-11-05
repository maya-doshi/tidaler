"""Validated ISRC lookups through the authenticated TIDAL client."""

import re
from contextlib import nullcontext
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from tidalapi.exceptions import ObjectNotFound
from tidalapi.media import Track
from tidalapi.session import Session
from urllib3.util.retry import Retry

from tidaler.constants import REQUESTS_TIMEOUT_SEC


def normalize_isrc(value: str) -> str:
    """Normalize a compact or hyphenated ISRC.

    Args:
        value (str): ISRC, optionally prefixed with 'isrc:'.

    Returns:
        str: Uppercase, twelve-character ISRC.

    Raises:
        ValueError: If the input is not a valid ISRC format.
    """
    normalized: str = value.strip().upper()
    if normalized.startswith("ISRC:"):
        normalized = normalized[5:].strip()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}|[A-Z]{2}-[A-Z0-9]{3}-[0-9]{2}-[0-9]{5}", normalized):
        message: str = "Use a 12-character ISRC, for example USSM12209515 or US-SM1-22-09515."
        raise ValueError(message)
    return normalized.replace("-", "")


class _LookupHTTP(requests.Session):
    """Bound lookup requests with a timeout and retries for transient GET failures."""

    def __init__(self) -> None:
        """Reuse connections and retry transient server errors without retrying rate limits immediately."""
        super().__init__()
        self.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=2,
                    backoff_factor=0.5,
                    status_forcelist=(500, 502, 503, 504),
                    allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=False,
                )
            ),
        )

    def request(self, method: str, url: str | bytes, *args: Any, **kwargs: Any) -> requests.Response:
        """Send a request using the project timeout unless one is supplied."""
        if len(args) < 7:  # requests also permits timeout as a positional argument.
            kwargs.setdefault("timeout", REQUESTS_TIMEOUT_SEC)
        return super().request(method, url, *args, **kwargs)


def tracks_by_isrc(session: Session, value: str, reuse_transport: bool = False) -> list[Track]:
    """Find distinct, playable tracks whose ISRC exactly matches the input.

    Args:
        session (Session): Authenticated TIDAL session; its country is used for lookup.
        value (str): Compact or hyphenated ISRC.
        reuse_transport (bool): Reuse a bounded batch transport when already installed.

    Returns:
        list[Track]: Matching tracks, in API order, without duplicate IDs.

    Raises:
        ValueError: If the input is malformed.
        requests.RequestException: If the network request fails.
        tidalapi.exceptions.TidalAPIError: If TIDAL rejects the lookup.
    """
    isrc: str = normalize_isrc(value)
    original_http: requests.Session = session.request_session
    # This helper is used synchronously by the CLI. Keep its transport policy
    # local to the lookup so downloads and the GUI retain their own policies.
    with nullcontext(original_http) if reuse_transport else _LookupHTTP() as http:
        retries: Retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=False,
        )
        http.mount("https://", HTTPAdapter(max_retries=retries))
        session.request_session = http
        try:
            try:
                matches: list[Track] = session.get_tracks_by_isrc(isrc)
            except ObjectNotFound:
                return []
        finally:
            session.request_session = original_http

    unique: dict[int, Track] = {}
    for track in matches:
        if track.id is None or not track.available or not track.isrc:
            continue
        try:
            matches_isrc: bool = normalize_isrc(track.isrc) == isrc
        except ValueError:
            continue
        if matches_isrc:
            unique.setdefault(track.id, track)
    return list(unique.values())
