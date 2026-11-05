"""Regression tests for ISRC lookup, CLI output, and safe track selection."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from tidalapi import Session, Track
from tidalapi.exceptions import InvalidISRC, ObjectNotFound, TooManyRequests
from typer.testing import CliRunner

from tidaler import cli
from tidaler.helper.isrc import _LookupHTTP, normalize_isrc, tracks_by_isrc

ISRC = "USSM12209515"


def make_track(track_id: int = 123, isrc: str = ISRC, available: bool = True) -> Mock:
    """Build a track without making network requests."""
    track: Mock = Mock(spec=Track)
    track.id = track_id
    track.isrc = isrc
    track.available = available
    track.name = "Example"
    track.artists = [SimpleNamespace(name="Artist")]
    track.album = SimpleNamespace(name="Album")
    return track


@pytest.mark.parametrize("value", [ISRC, " ussm12209515 ", "US-SM1-22-09515", "isrc: us-sm1-22-09515"])
def test_normalizes_supported_formats(value: str) -> None:
    """Accept canonical, lowercase, prefixed, and formatted codes."""
    assert normalize_isrc(value) == ISRC


@pytest.mark.parametrize("value", ["", "123", "USSM122095150", "1SSM12209515", "USSM1AA09515", "US--SM12209515"])
def test_rejects_invalid_codes(value: str) -> None:
    """Reject malformed codes before login or network calls."""
    with pytest.raises(ValueError):
        normalize_isrc(value)


def test_filters_mismatches_unavailable_and_duplicate_tracks() -> None:
    """Never return an unavailable recording or a different ISRC."""
    session: Mock = Mock(spec=Session)
    original: Mock = Mock()
    session.request_session = original
    session.get_tracks_by_isrc.return_value = [
        make_track(),
        make_track(),
        make_track(234, available=False),
        make_track(345, "USSM12209516"),
        make_track(456, "bad"),
        make_track(567),
    ]
    assert [track.id for track in tracks_by_isrc(session, "US-SM1-22-09515")] == [123, 567]
    session.get_tracks_by_isrc.assert_called_once_with(ISRC)
    assert session.request_session is original


@pytest.mark.parametrize("error", [ObjectNotFound(), InvalidISRC(), requests.Timeout(), TooManyRequests()])
def test_restores_transport_after_error(error: Exception) -> None:
    """Restore the original authenticated session transport on every error."""
    session: Mock = Mock(spec=Session)
    original: Mock = Mock()
    session.request_session = original
    session.get_tracks_by_isrc.side_effect = error
    if isinstance(error, ObjectNotFound):
        assert tracks_by_isrc(session, ISRC) == []
    else:
        with pytest.raises(type(error)):
            tracks_by_isrc(session, ISRC)
    assert session.request_session is original


def test_calls_real_library_isrc_endpoint() -> None:
    """Verify integration with tidalapi's OpenAPI v2 filter and track hydration."""
    session: Session = Session()
    track: Mock = make_track()
    response: Mock = Mock()
    response.json.return_value = {"data": [{"id": "123"}]}
    try:
        with (
            patch.object(session.request, "request", return_value=response) as request,
            patch.object(session, "track", return_value=track),
        ):
            assert tracks_by_isrc(session, ISRC) == [track]
        request.assert_called_once_with(
            "GET", "tracks", params={"filter[isrc]": ISRC}, base_url="https://openapi.tidal.com/v2/"
        )
    finally:
        session.request_session.close()


def test_lookup_http_supplies_timeout() -> None:
    """Ensure lookup transport cannot wait indefinitely."""
    with _LookupHTTP() as http, patch.object(requests.Session, "request") as request:
        http.request("GET", "https://openapi.tidal.com/v2/tracks")
        assert request.call_args.kwargs["timeout"] == 45
        http.request("GET", "https://openapi.tidal.com/v2/tracks", timeout=3)
        assert request.call_args.kwargs["timeout"] == 3


@pytest.fixture
def lookup(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Stub login and catalogue access while exercising the real Typer command."""

    def login(ctx: object) -> bool:
        """Simulate login chatter and attach a session without storing credentials."""
        print("Login status")
        ctx.obj["tidal"] = SimpleNamespace(session=object())
        return True

    monkeypatch.setattr(cli, "login", login)
    mock: Mock = Mock(return_value=[make_track()])
    monkeypatch.setattr(cli, "tracks_by_isrc", mock)
    return mock


def test_cli_prints_only_urls_to_stdout(lookup: Mock) -> None:
    """Login chatter must not break shell pipelines."""
    result = CliRunner().invoke(cli.app, ["isrc", "isrc:us-sm1-22-09515"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "https://tidal.com/browse/track/123\n"
    assert "Login status" in result.stderr
    assert lookup.call_args.args[1] == ISRC


def test_cli_json_output(lookup: Mock) -> None:
    """JSON must remain valid when login prints messages."""
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "id": 123,
            "isrc": ISRC,
            "title": "Example",
            "artists": ["Artist"],
            "album": "Album",
            "url": "https://tidal.com/browse/track/123",
        }
    ]


def test_cli_invalid_input_never_logs_in(lookup: Mock) -> None:
    """Reject invalid codes before authentication."""
    result = CliRunner().invoke(cli.app, ["isrc", "bad"])
    assert result.exit_code == 2
    assert "Login status" not in result.output
    lookup.assert_not_called()


def test_cli_missing_match_has_nonzero_exit_and_empty_json(lookup: Mock) -> None:
    """Scripts can distinguish no match without parsing human text."""
    lookup.return_value = []
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == []
    assert "No playable" in result.stderr


@pytest.mark.parametrize("error", [InvalidISRC("SECRET"), requests.Timeout("SECRET"), TooManyRequests()])
def test_cli_api_errors_are_safe(lookup: Mock, error: Exception) -> None:
    """Errors must be actionable and must not expose raw server messages."""
    lookup.side_effect = error
    result = CliRunner().invoke(cli.app, ["isrc", ISRC])
    assert result.exit_code == 1
    assert not result.stdout
    assert "SECRET" not in result.output
    assert "lookup" in result.stderr or "rate limit" in result.stderr


def test_cli_failed_login_stops_lookup(lookup: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never query an unauthenticated session after login failure."""
    monkeypatch.setattr(cli, "login", lambda ctx: False)
    result = CliRunner().invoke(cli.app, ["isrc", ISRC])
    assert result.exit_code == 1
    lookup.assert_not_called()


def test_cli_ambiguous_download_requires_selection(lookup: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not silently choose an edition from several matches."""
    lookup.return_value = [make_track(), make_track(456)]
    download: Mock = Mock()
    monkeypatch.setattr(cli, "_download", download)
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--download"])
    assert result.exit_code == 1
    assert "--track-id" in result.stderr
    download.assert_not_called()


def test_cli_selected_download_reuses_login(lookup: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward only the explicitly selected match to the existing downloader."""
    lookup.return_value = [make_track(), make_track(456)]
    download: Mock = Mock(return_value=True)
    monkeypatch.setattr(cli, "_download", download)
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--download", "--track-id", "456"])
    assert result.exit_code == 0, result.output
    assert download.call_args.args[1] == ["https://tidal.com/browse/track/456"]
    assert download.call_args.kwargs == {"try_login": False}


def test_cli_wrong_selection_does_not_download(lookup: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrelated track ID must not be downloaded."""
    download: Mock = Mock()
    monkeypatch.setattr(cli, "_download", download)
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--download", "--track-id", "999"])
    assert result.exit_code == 1
    download.assert_not_called()


def test_cli_single_match_can_download(lookup: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unique match needs no explicit selection."""
    download: Mock = Mock(return_value=True)
    monkeypatch.setattr(cli, "_download", download)
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--download"])
    assert result.exit_code == 0, result.output
    download.assert_called_once()


def test_cli_rejects_json_download_combination(lookup: Mock) -> None:
    """Download progress cannot contaminate JSON output."""
    result = CliRunner().invoke(cli.app, ["isrc", ISRC, "--json", "--download"])
    assert result.exit_code == 2
    lookup.assert_not_called()
