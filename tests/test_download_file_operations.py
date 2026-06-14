import pathlib
from unittest.mock import MagicMock, patch

import pytest

from tidaler.download import Download


@pytest.fixture
def download_instance() -> Download:
    """Create a Download instance for file operation tests.

    Returns:
        Download: Configured download instance.
    """
    downloader = Download.__new__(Download)
    downloader.fn_logger = MagicMock()
    downloader._FILE_OPERATION_RETRIES = 2
    downloader._FILE_OPERATION_RETRY_DELAY_SEC = 0

    return downloader


def test_move_file_retries_transient_replace_failure(download_instance: Download, tmp_path: pathlib.Path) -> None:
    """Verify overwrite moves are retried when the destination is temporarily locked.

    Args:
        download_instance (Download): Download instance under test.
        tmp_path (pathlib.Path): Temporary test directory.
    """
    source_path: pathlib.Path = tmp_path / "source.flac"
    destination_path: pathlib.Path = tmp_path / "destination.flac"
    source_path.write_text("new", encoding="utf-8")
    destination_path.write_text("old", encoding="utf-8")

    replace_original = pathlib.Path.replace
    replace_calls: int = 0

    def replace_once_locked(self: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return replace_original(self, target)

    with patch.object(pathlib.Path, "replace", replace_once_locked):
        result: bool = download_instance._move_file(source_path, destination_path, overwrite=True)

    assert result is True
    assert destination_path.read_text(encoding="utf-8") == "new"
    assert not source_path.exists()
    assert replace_calls == 2


def test_move_file_skip_existing_keeps_destination(download_instance: Download, tmp_path: pathlib.Path) -> None:
    """Verify shared album extras are skipped when another track already wrote them.

    Args:
        download_instance (Download): Download instance under test.
        tmp_path (pathlib.Path): Temporary test directory.
    """
    source_path: pathlib.Path = tmp_path / "cover-source.jpg"
    destination_path: pathlib.Path = tmp_path / "cover.jpg"
    source_path.write_bytes(b"new-cover")
    destination_path.write_bytes(b"existing-cover")

    result: bool = download_instance._move_file(
        source_path,
        destination_path,
        overwrite=False,
        skip_if_exists=True,
    )

    assert result is True
    assert destination_path.read_bytes() == b"existing-cover"
    assert not source_path.exists()
