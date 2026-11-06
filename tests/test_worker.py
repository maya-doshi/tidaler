"""Sustained-session, shared cooldown, protocol, and token preservation regressions."""

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from tidaler import integration_worker as worker
from tidaler.helper import isrc_batch, search_batch
from tidaler.helper.worker_http import RequestGate, WorkerError, WorkerHTTP

CODE = "TCAFX2133359"


@pytest.fixture
def sustained(tmp_path: Path) -> Any:
    """Create an isolated worker with fake credentials and a mocked API session."""
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token_type": "Bearer", "access_token": "test-only", "refresh_token": "test-only"}))
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    settings = SimpleNamespace(data=SimpleNamespace(quality_audio="HI_RES_LOSSLESS"))
    session = Mock(expiry_time=datetime.now(UTC) + timedelta(hours=1), country_code="IN")
    session.load_oauth_session.return_value = True
    tidal = Mock(session=session, settings=settings)
    with (
        patch.object(worker, "path_config_base", return_value=str(tmp_path)),
        patch.object(worker, "path_file_token", return_value=str(token)),
        patch.object(worker, "path_file_settings", return_value=str(settings_file)),
        patch.object(worker, "Settings", return_value=settings),
        patch.object(worker, "Tidal", return_value=tidal),
    ):
        instance = worker.Worker("lookup", Mock(obj={}))
        try:
            yield instance, session, token
        finally:
            instance.close()


def test_mixed_lookup_jobs_load_login_once_and_reuse_transport(sustained: Any) -> None:
    """ISRC and title searches share the same authenticated object across requests."""
    instance, session, _ = sustained
    with (
        patch.object(worker, "resolve_batch", return_value={CODE: []}),
        patch.object(worker, "search_batch", return_value={"s": []}),
    ):
        for request in [
            {"action": "isrc", "codes": [CODE]},
            {"action": "search", "jobs": [{"id": "s", "title": "Song"}]},
            {"action": "isrc", "codes": [CODE]},
        ]:
            instance.handle(request, Mock())
    assert session.load_oauth_session.call_count == 1
    assert session.request_session is instance.http
    session.check_login.assert_not_called()
    session.token_refresh.assert_not_called()


def test_only_expired_token_refreshes_without_another_login(sustained: Any) -> None:
    """Refreshing an expired access token doesn't start another login verification sequence."""
    instance, session, _ = sustained
    instance.prepare()
    session.expiry_time = datetime.now(UTC) - timedelta(seconds=1)

    def refreshed(_: str) -> bool:
        session.expiry_time = datetime.now(UTC) + timedelta(hours=1)
        return True

    session.token_refresh.side_effect = refreshed
    instance.prepare()
    instance.prepare()
    session.token_refresh.assert_called_once()
    session.load_oauth_session.assert_called_once()


def test_failed_authentication_load_preserves_token_file(sustained: Any) -> None:
    """A forbidden response must never delete the saved token."""
    instance, session, token = sustained
    before = token.read_bytes()
    session.load_oauth_session.side_effect = WorkerError("forbidden", "Forbidden", 600)
    with pytest.raises(WorkerError):
        instance.prepare()
    assert token.read_bytes() == before
    assert not instance.authenticated


def test_rewriting_identical_credentials_does_not_repeat_login(sustained: Any) -> None:
    """The other worker's config initialization can rewrite a token file without changing the session."""
    instance, session, token = sustained
    instance.prepare()
    token.write_text(json.dumps(json.loads(token.read_text()), indent=4))
    instance.token_stamp = -1
    instance.prepare()
    session.load_oauth_session.assert_called_once()


def test_positive_title_cache_and_refresh_override(sustained: Any) -> None:
    """Repeat title searches reuse successful metadata, while explicit retry bypasses it."""
    instance, _, _ = sustained
    request = {"action": "search", "jobs": [{"id": "s", "title": "Song", "artist": "Artist"}]}
    with patch.object(worker, "search_batch", return_value={"s": [{"id": 1, "isrc": CODE}]}) as search:
        instance.handle(request, Mock())
        instance.handle(request, Mock())
        assert search.call_count == 1
        instance.handle({**request, "refresh": True}, Mock())
        assert search.call_count == 2


def test_empty_title_results_are_not_cached(sustained: Any) -> None:
    """Absence of a match doesn't create a sustained-session negative cache."""
    instance, _, _ = sustained
    with patch.object(worker, "search_batch", return_value={"s": []}) as search:
        for _ in range(2):
            instance.handle({"action": "search", "jobs": [{"id": "s", "title": "Song"}]}, Mock())
    assert search.call_count == 2


def test_lookup_helpers_keep_persistent_transport(tmp_path: Path) -> None:
    """Helpers must not replace the worker's pacing transport with an unpaced temporary session."""
    session = Mock(country_code="IN", user=SimpleNamespace(id=1))
    session.request_session = Mock(persistent=True)
    original = session.request_session
    with (
        patch.object(isrc_batch, "_batch_candidates", return_value={CODE: []}),
        patch.object(isrc_batch, "tracks_by_isrc", return_value=[]),
        patch.object(isrc_batch, "_LookupHTTP") as temporary,
    ):
        isrc_batch.resolve_batch(session, [CODE], "LOSSLESS", tmp_path / "cache.db")
        temporary.assert_not_called()
    with (
        patch.object(search_batch, "search_tracks", return_value=[]),
        patch.object(search_batch, "_LookupHTTP") as temporary,
    ):
        search_batch.search_batch(session, [{"id": "s", "title": "Song"}])
        temporary.assert_not_called()
    assert session.request_session is original


@pytest.mark.parametrize("status,seconds", [(403, 600), (429, 60)])
def test_rejection_blocks_both_workers_without_network_retry(tmp_path: Path, status: int, seconds: int) -> None:
    """Both processes read the same gate; 403/429 trigger one request, then local rejection."""
    gate = RequestGate(tmp_path / "gate.db", interval=0)
    other = RequestGate(tmp_path / "gate.db", interval=0)
    response = Mock(status_code=status, headers={})
    with WorkerHTTP(gate) as http, patch.object(requests.Session, "request", return_value=response) as send:
        with pytest.raises(WorkerError) as failure:
            http.get("https://api.tidal.com/v1/tracks/1")
        assert failure.value.retry_after >= seconds
        with pytest.raises(WorkerError):
            http.get("https://api.tidal.com/v1/tracks/2")
        with pytest.raises(WorkerError):
            other.check()
        send.assert_called_once()
    response.close.assert_called_once()


def test_retry_after_is_honored(tmp_path: Path) -> None:
    """The shared cooldown cannot be shorter than the service's Retry-After."""
    gate = RequestGate(tmp_path / "gate.db")
    assert gate.block(429, "900").retry_after == 900


def test_two_workers_reserve_spaced_requests(tmp_path: Path) -> None:
    """Pacing is shared rather than doubled when lookup and download run together."""
    first = RequestGate(tmp_path / "gate.db")
    second = RequestGate(tmp_path / "gate.db")
    with patch("tidaler.helper.worker_http.time.sleep") as sleep:
        first.wait()
        second.wait()
    assert sleep.call_args.args[0] > 0.5


def test_401_refresh_retry_is_bounded(tmp_path: Path) -> None:
    """Let tidalapi attempt its expired-token refresh once; a second 401 stops the request."""
    with WorkerHTTP(RequestGate(tmp_path / "gate.db", interval=0)) as http:
        response = Mock(status_code=401)
        with patch.object(requests.Session, "request", return_value=response):
            assert http.get("https://api.tidal.com/v1/tracks/1") is response
            with pytest.raises(WorkerError, match="authentication failed"):
                http.get("https://api.tidal.com/v1/tracks/1")


def test_protocol_recovers_from_bad_input_and_sanitizes_errors(sustained: Any) -> None:
    """Malformed requests and private upstream errors can't corrupt protocol output or leak secrets."""
    instance, _, _ = sustained
    output = io.StringIO()
    source = io.StringIO(
        'bad\n{"request_id":"a","action":"isrc","codes":["bad"]}\n{"request_id":"b","action":"isrc","codes":["TCAFX2133359"]}\n'
    )
    with (
        patch.object(worker, "Worker", return_value=instance),
        patch.object(worker, "resolve_batch", side_effect=requests.Timeout("private token in URL")),
    ):
        worker.serve("lookup", Mock(obj={}), source, output)
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [result["reason"] for result in results] == ["invalid_request", "invalid_request", "temporary"]
    assert "private token" not in output.getvalue()


def test_download_jobs_reuse_login_and_stop_on_cooldown(sustained: Any, tmp_path: Path) -> None:
    """Download-session reuse spans batches, with per-job completion and no further jobs after a 403."""
    instance, session, _ = sustained
    instance.role = "download"
    job = {"id": "s", "track_id": 1, "isrc": CODE, "folder": str(tmp_path)}
    with (
        patch("tidaler.cli._prepare_resolved_download", return_value="LOSSLESS"),
        patch.object(worker, "resolved_track", return_value=Mock(id=1)),
        patch("tidaler.cli._download", return_value=True) as download,
    ):
        instance.handle({"action": "download", "jobs": [job]}, Mock())
        instance.handle({"action": "download", "jobs": [job]}, Mock())
        session.load_oauth_session.assert_called_once()
        assert download.call_count == 2
        download.side_effect = WorkerError("forbidden", "Forbidden", 600)
        event = Mock()
        with pytest.raises(WorkerError):
            instance.handle({"action": "download", "jobs": [job, {**job, "id": "later"}]}, event)
        assert [call.args[0]["stage"] for call in event.call_args_list] == ["validating", "downloading"]


def test_download_event_reports_only_completed_files(sustained: Any, tmp_path: Path) -> None:
    """The protocol forwards the downloader's final file path and doesn't guess or scan a directory."""
    instance, _, _ = sustained
    instance.role = "download"
    destination = str(tmp_path / "actual.flac")

    def download(*args: Any, **kwargs: Any) -> bool:
        kwargs["completed_files"].append(destination)
        return True

    with (
        patch("tidaler.cli._prepare_resolved_download", return_value="LOSSLESS"),
        patch.object(worker, "resolved_track", return_value=Mock(id=1)),
        patch("tidaler.cli._download", side_effect=download),
    ):
        event = Mock()
        instance.handle(
            {"action": "download", "jobs": [{"id": "song", "track_id": 1, "isrc": CODE, "folder": str(tmp_path)}]},
            event,
        )
    assert event.call_args.args[0]["files"] == [destination]


def test_wrong_role_never_authenticates(sustained: Any) -> None:
    """The lookup worker cannot accidentally start a download."""
    instance, session, _ = sustained
    with pytest.raises(ValueError):
        instance.handle({"action": "download", "jobs": []}, Mock())
    session.load_oauth_session.assert_not_called()
