"""Final-path and completion callback regressions without network or real audio."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tidalapi import Track

from tidaler.download import Download


def test_stream_extension_propagates_to_final_result(tmp_path: Path) -> None:
    """A stream's FLAC extension replaces the guessed extension in the returned destination."""
    downloader: Any = Mock()
    downloader._get_stream_info.return_value = (Mock(), ".flac", True, Mock())
    downloader._perform_actual_download.return_value = True
    result = Download._download_and_process_media(
        downloader, Mock(spec=Track), tmp_path / "song.m4a", False, False, ".m4a"
    )
    assert result == (True, tmp_path / "song.flac")
    assert downloader._perform_actual_download.call_args.args[1] == tmp_path / "song.flac"


@pytest.mark.parametrize("skip,success", [(False, True), (True, True), (False, False)])
def test_item_reports_completed_output_only(tmp_path: Path, skip: bool, success: bool) -> None:
    """Only completed transfers report a path; existing-file skips and failures report nothing."""
    downloader: Any = Mock()
    media = Mock(spec=Track)
    destination = tmp_path / "song.flac"
    downloader.event_abort.is_set.return_value = False
    downloader._validate_and_prepare_media.return_value = media
    downloader._prepare_file_paths_and_skip_logic.return_value = (tmp_path / "song.m4a", ".m4a", skip, False)
    downloader._adjust_quality_settings.return_value = (None, None)
    downloader._download_and_process_media.return_value = (success, destination)
    downloader._perform_post_processing.return_value = destination
    callback = Mock()
    Download.item(downloader, "template", media=media, on_completed=callback)
    if success and not skip:
        callback.assert_called_once_with(destination.resolve())
        assert downloader._perform_post_processing.call_args.args[1] == destination
    else:
        callback.assert_not_called()
