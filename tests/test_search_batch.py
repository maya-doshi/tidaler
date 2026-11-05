"""Title/artist search for songs without an ISRC."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from tidalapi import Session, Track
from tidalapi.exceptions import TooManyRequests
from typer.testing import CliRunner

from tidaler import cli
from tidaler.helper import search_batch as search

CODE = "USUG12002844"


def track(identity: int = 1, code: str | None = CODE, available: bool = True, version: str | None = None) -> Mock:
    """Create a searchable track with the fields search records."""
    result = Mock(spec=Track)
    result.id, result.isrc, result.available = identity, code, available
    result.audio_quality, result.media_metadata_tags = "LOSSLESS", []
    result.name, result.version, result.duration = "Song", version, 201
    result.album, result.artists = SimpleNamespace(name="Album"), [SimpleNamespace(name="Artist")]
    return result


def session(*answers: list[Mock]) -> Mock:
    """A session whose successive searches return these track lists."""
    result = Mock(spec=Session)
    result.country_code = "IN"
    result.request_session = Mock()
    result.search.side_effect = [{"tracks": tracks} for tracks in answers]
    return result


def test_queries_try_the_title_without_brackets() -> None:
    """TIDAL keeps "(Taylor's Version)" in a separate field, so the second query drops it."""
    assert search.queries("Delicate (Taylor's Version)", "Taylor Swift") == [
        "Delicate (Taylor's Version) Taylor Swift",
        "Delicate Taylor Swift",
    ]
    assert search.queries("Delicate", "Taylor Swift") == ["Delicate Taylor Swift"]


def test_search_records_version_duration_and_skips_unusable_tracks() -> None:
    """Unavailable tracks, tracks without an ISRC and duplicates are left out."""
    client = session([track(1, version="Taylor's Version"), track(2, available=False), track(3, code=None), track(1)])
    records = search.search_tracks(client, "Delicate", "Taylor Swift")
    assert [record["id"] for record in records] == [1]
    assert records[0]["version"] == "Taylor's Version"
    assert records[0]["duration"] == 201
    assert records[0]["isrc"] == CODE
    assert records[0]["quality_rank"] == 2


def test_falls_back_to_the_short_query_only_when_the_full_one_finds_nothing() -> None:
    """One request when the full title is found; a second only after an empty answer."""
    found = session([track(1)])
    assert search.search_tracks(found, "Song (Live)", "Artist")
    assert found.search.call_count == 1
    empty_first = session([], [track(2)])
    assert [record["id"] for record in search.search_tracks(empty_first, "Song (Live)", "Artist")] == [2]
    assert empty_first.search.call_count == 2


def test_batch_restores_the_transport_and_keys_results_by_job() -> None:
    """Every job gets an entry, even when nothing is found."""
    client = session([track(1)], [], [])
    original = client.request_session
    results = search.search_batch(client, [{"id": "a", "title": "Song", "artist": "X"}, {"id": "b", "title": "Gone"}], 0)
    assert set(results) == {"a", "b"}
    assert results["b"] == []
    assert client.request_session is original


def test_rate_limit_propagates_instead_of_reporting_no_match() -> None:
    """A refused search must not look like "TIDAL doesn't have it"."""
    client = Mock(spec=Session)
    client.request_session = Mock()
    client.search.side_effect = TooManyRequests("slow down")
    with pytest.raises(TooManyRequests):
        search.search_batch(client, [{"id": "a", "title": "Song"}], 0)


def test_cli_logs_in_once_and_prints_json(tmp_path: Path) -> None:
    """stdout stays a single JSON document keyed by job id."""
    manifest = tmp_path / "songs.json"
    manifest.write_text(json.dumps([{"id": "a", "title": "Song", "artist": "X"}, {"id": "b", "title": "Other"}]))
    client = session([track(1)], [track(2)])
    login = Mock(side_effect=lambda ctx: ctx.obj.update(tidal=SimpleNamespace(session=client)))
    with patch.object(cli, "_integration_login", login):
        result = CliRunner().invoke(cli.app, ["search-batch", "--list", str(manifest), "--delay", "0"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert [record["id"] for record in data["results"]["a"]] == [1]
    assert data["country"] == "IN"
    assert login.call_count == 1


def test_cli_rejects_songs_without_titles_before_login(tmp_path: Path) -> None:
    """Malformed input never authenticates."""
    manifest = tmp_path / "songs.json"
    manifest.write_text('[{"id": "a", "title": "  "}]')
    with patch.object(cli, "_integration_login") as login:
        result = CliRunner().invoke(cli.app, ["search-batch", "--list", str(manifest)])
    assert result.exit_code == 2
    login.assert_not_called()


def test_cli_reports_rate_limits_without_details(tmp_path: Path) -> None:
    """A rate limit exits 1 with a short message, never server details."""
    manifest = tmp_path / "songs.json"
    manifest.write_text('[{"id": "a", "title": "Song"}]')
    client = Mock(spec=Session)
    client.request_session = Mock()
    client.search.side_effect = TooManyRequests("private detail")
    login = Mock(side_effect=lambda ctx: ctx.obj.update(tidal=SimpleNamespace(session=client)))
    with patch.object(cli, "_integration_login", login):
        result = CliRunner().invoke(cli.app, ["search-batch", "--list", str(manifest), "--delay", "0"])
    assert result.exit_code == 1
    assert "rate limit" in result.output
    assert "private detail" not in result.output
