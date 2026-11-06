"""Batch, cache, and resolved-download regression tests without network or account state."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
from tidalapi import Session, Track
from typer.testing import CliRunner

from tidaler import cli
from tidaler.helper import isrc_batch as batch

CODE = "USSM12209515"


def track(code: str = CODE, quality: str = "HI_RES_LOSSLESS", identity: int = 123) -> Mock:
    """Create a playable track with advertised quality and metadata."""
    result = Mock(spec=Track)
    result.id, result.isrc, result.available = identity, code, True
    result.audio_quality, result.media_metadata_tags = quality, []
    result.name, result.album, result.artists = "Song", SimpleNamespace(name="Album"), []
    return result


def session() -> Mock:
    """Create an authenticated session stub with JSON API context-manager responses."""
    result = Mock(spec=Session)
    result.country_code = "US"
    result.user = SimpleNamespace(id=1)
    result.request_session = Mock()
    result.config = SimpleNamespace(openapi_v2_location="https://openapi.tidal.com/v2/")
    result.request = Mock()
    response = MagicMock()
    response.__enter__.return_value = response
    response.json.return_value = {"data": [{"id": "123"}]}
    result.request.request.return_value = response
    result.track.return_value = track()
    return result


def test_batches_twenty_codes_and_reuses_transport(tmp_path: Path) -> None:
    """Twenty-one codes need two filter requests, not twenty-one."""
    client = session()
    codes = [f"USSM1{2200000 + index:07d}" for index in range(21)]
    original = client.request_session
    with patch.object(
        batch, "_batch_candidates", side_effect=lambda _, values: {code: [track(code)] for code in values}
    ) as request:
        results = batch.resolve_batch(client, codes + codes[:2], "HI_RES_LOSSLESS", tmp_path / "cache.db")
    assert len(results) == 21
    assert [len(call.args[1]) for call in request.call_args_list] == [20, 1]
    assert client.request_session is original


def test_batch_request_filters_exact_isrc(tmp_path: Path) -> None:
    """Use the v2 filter and verify returned candidates before caching."""
    client = session()
    result = batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", tmp_path / "cache.db")
    assert result[CODE][0]["id"] == 123
    assert client.request.request.call_args.kwargs["params"] == {"filter[isrc]": CODE}
    client.track.assert_called_once_with("123")


@pytest.mark.parametrize(
    "quality,available,isrc", [("LOSSLESS", True, CODE), ("HIGH", False, CODE), ("HIGH", True, "USSM12209516")]
)
def test_falls_back_for_quality_unavailability_or_mismatch(
    tmp_path: Path, quality: str, available: bool, isrc: str
) -> None:
    """A batch's single edition must not silently reduce quality or change identity."""
    client = session()
    client.track.return_value = track(isrc, quality)
    client.track.return_value.available = available
    with patch.object(batch, "tracks_by_isrc", return_value=[track(identity=456)]) as lookup:
        result = batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", tmp_path / "cache.db")
    lookup.assert_called_once_with(client, CODE, reuse_transport=True)
    assert result[CODE][0]["id"] == 456


def test_cache_hit_needs_no_catalog_requests(tmp_path: Path) -> None:
    """A fresh positive result avoids API work on the next invocation."""
    client = session()
    path = tmp_path / "cache.db"
    batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", path)
    client.request.request.reset_mock()
    client.track.reset_mock()
    assert batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", path)[CODE]
    client.request.request.assert_not_called()
    client.track.assert_not_called()


@pytest.mark.parametrize("change", ["country", "user", "quality", "refresh", "expired"])
def test_cache_scope_expiry_and_refresh(tmp_path: Path, change: str) -> None:
    """Another account, territory, quality, explicit refresh, or expiry forces a lookup."""
    client = session()
    path = tmp_path / "cache.db"
    batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", path)
    quality = "HI_RES_LOSSLESS"
    if change == "country":
        client.country_code = "IN"
    elif change == "user":
        client.user.id = 2
    elif change == "quality":
        quality = "LOSSLESS"
    now = batch.time.time() + (batch.CACHE_SECONDS + 1 if change == "expired" else 0)
    with patch.object(batch.time, "time", return_value=now):
        batch.resolve_batch(client, [CODE], quality, path, refresh=change == "refresh")
    assert client.request.request.call_count == 2


def test_api_error_is_not_cached_as_no_match(tmp_path: Path) -> None:
    """Network failures propagate, restoring transport without creating negative cache entries."""
    client = session()
    original = client.request_session
    client.request.request.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        batch.resolve_batch(client, [CODE], "HI_RES_LOSSLESS", tmp_path / "cache.db")
    assert client.request_session is original
    assert not (tmp_path / "cache.db").exists()


def test_resolved_track_skips_duplicate_isrc_search(tmp_path: Path) -> None:
    """Direct validation of a playable high-resolution ID needs no ISRC lookup."""
    client = session()
    with patch.object(batch, "tracks_by_isrc") as lookup:
        assert batch.resolved_track(client, 123, CODE, "HI_RES_LOSSLESS", tmp_path / "cache.db").id == 123
    lookup.assert_not_called()


def test_cached_best_standard_lossless_does_not_repeat_quality_search(tmp_path: Path) -> None:
    """Remember that an individual fallback already checked available editions."""
    client = session()
    client.track.return_value = track(quality="LOSSLESS")
    path = tmp_path / "cache.db"
    cache = batch.match_cache(client, "HI_RES_LOSSLESS", path)
    cache.write(CODE, [batch.track_record(client.track.return_value)])
    with patch.object(batch, "tracks_by_isrc") as lookup:
        assert batch.resolved_track(client, 123, CODE, "HI_RES_LOSSLESS", path).id == 123
    lookup.assert_not_called()


def test_stale_resolved_id_rechecks_and_updates_cache(tmp_path: Path) -> None:
    """Unavailable cached editions must be replaced by an exact-ISRC match."""
    client = session()
    client.track.return_value.available = False
    path = tmp_path / "cache.db"
    with patch.object(batch, "tracks_by_isrc", return_value=[track(identity=456)]):
        assert batch.resolved_track(client, 123, CODE, "HI_RES_LOSSLESS", path).id == 456
    assert batch.match_cache(client, "HI_RES_LOSSLESS", path).read(CODE)[0]["id"] == 456


def test_cached_quality_drop_checks_for_a_better_edition(tmp_path: Path) -> None:
    """A cached ID cannot hide a subsequent drop in advertised quality."""
    client = session()
    path = tmp_path / "cache.db"
    batch.match_cache(client, "HI_RES_LOSSLESS", path).write(CODE, [batch.track_record(track())])
    client.track.return_value = track(quality="LOSSLESS")
    with patch.object(batch, "tracks_by_isrc", return_value=[track(identity=456)]) as lookup:
        assert batch.resolved_track(client, 123, CODE, "HI_RES_LOSSLESS", path).id == 456
    lookup.assert_called_once()


def test_track_handler_propagates_failed_download() -> None:
    """A failed media transfer must not emit a successful resolved-download result."""
    downloader = Mock()
    downloader.item.return_value = (False, "")
    context = Mock(obj={cli.CTX_TIDAL: Mock()})
    assert cli._handle_track_or_video(downloader, context, "url", track(), "template", 0, 0) is False


def test_bulk_configuration_failure_is_identified(tmp_path: Path) -> None:
    """Folder/FLAC configuration failures must not permanently block the song."""
    job = {"id": "one", "folder": str(tmp_path)}
    with (
        patch.object(cli, "_prepare_resolved_download", side_effect=ValueError()),
        patch.object(cli, "_batch_event") as event,
    ):
        cli._batch_job(Mock(), job)
    assert event.call_args.kwargs["error"].startswith("Tidaler configuration error:")


def test_bulk_cli_logs_in_once_and_reports_individual_failures(tmp_path: Path) -> None:
    """A failed song does not lose the next song, and stdout stays JSON-lines."""
    manifest = tmp_path / "jobs.json"
    manifest.write_text(
        json.dumps([{"id": str(index), "track_id": 123, "isrc": CODE, "folder": str(tmp_path)} for index in range(3)])
    )
    login = Mock(side_effect=lambda ctx: ctx.obj.update(tidal=SimpleNamespace(session=session())))
    with (
        patch.object(cli, "_integration_login", login),
        patch.object(cli, "_prepare_resolved_download", return_value="HI_RES_LOSSLESS"),
        patch.object(cli, "resolved_track", return_value=track()),
        patch.object(cli, "_download", side_effect=[True, ValueError("private server detail"), True]),
    ):
        result = CliRunner().invoke(cli.app, ["download-batch", "--list", str(manifest)])
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["status"] for event in events] == ["running", "done", "running", "error", "running", "done"]
    assert "private server detail" not in result.output
    assert login.call_count == 1


def test_bulk_cli_rejects_invalid_job_before_login(tmp_path: Path) -> None:
    """Malformed manifests never authenticate or download."""
    manifest = tmp_path / "jobs.json"
    manifest.write_text('[{"isrc":"bad"}]')
    with patch.object(cli, "_integration_login") as login:
        result = CliRunner().invoke(cli.app, ["download-batch", "--list", str(manifest)])
    assert result.exit_code == 2
    login.assert_not_called()
