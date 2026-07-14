import pathlib
from unittest.mock import call, patch

import pytest

from tidaler.helper.path import check_file_exists


class TestCheckFileExistsCaseSensitive:

    @pytest.fixture
    def audio_path(self) -> pathlib.Path:
        return pathlib.Path("/some/path/file.flac")

    def test_exact_match_found(self, audio_path):
        with patch("tidaler.helper.path.os.path.isfile", return_value=True):
            assert check_file_exists(audio_path) is True

    def test_exact_match_not_found(self, audio_path):
        with patch("tidaler.helper.path.os.path.isfile", return_value=False):
            assert check_file_exists(audio_path) is False

    def test_case_mismatch_not_found(self, audio_path):
        with patch("tidaler.helper.path.os.path.isfile", return_value=False):
            assert check_file_exists(audio_path) is False

    def test_directory_not_found(self):
        with patch("tidaler.helper.path.os.path.isfile", return_value=False):
            assert check_file_exists(pathlib.Path("/some/path")) is False

    def test_extension_ignore_found(self, audio_path):
        side_effects = [False, False, True] + [False] * 10
        with patch("tidaler.helper.path.os.path.isfile", side_effect=side_effects):
            assert check_file_exists(audio_path, extension_ignore=True) is True

    def test_extension_ignore_not_found(self, audio_path):
        with patch("tidaler.helper.path.os.path.isfile", return_value=False):
            assert check_file_exists(audio_path, extension_ignore=True) is False

    def test_extension_ignore_calls_all_extensions(self, audio_path):
        from tidalapi.media import AudioExtensions

        with patch("tidaler.helper.path.os.path.isfile", return_value=False) as mock_isfile:
            check_file_exists(audio_path, extension_ignore=True)
            stem = audio_path.stem
            expected_calls = [call(str(audio_path.parent / (stem + ext))) for ext in AudioExtensions]
            mock_isfile.assert_has_calls(expected_calls, any_order=True)
            assert mock_isfile.call_count == len(AudioExtensions)


class TestCheckFileExistsCaseInsensitive:

    @pytest.fixture
    def parent_dir(self, tmp_path: pathlib.Path) -> pathlib.Path:
        d = tmp_path / "music"
        d.mkdir()
        return d

    def test_exact_match_found(self, parent_dir):
        p = parent_dir / "Song.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["Song.flac"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is True

    def test_case_mismatch_still_found(self, parent_dir):
        p = parent_dir / "song.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["Song.flac"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is True

    def test_all_uppercase_vs_lowercase(self, parent_dir):
        p = parent_dir / "music.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["MUSIC.FLAC"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is True

    def test_nonexistent_file(self, parent_dir):
        p = parent_dir / "missing.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["Song.flac"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is False

    def test_extension_ignore_case_insensitive(self, parent_dir):
        p = parent_dir / "song.m4a"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["Song.flac"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False, extension_ignore=True) is True

    def test_empty_directory(self, parent_dir):
        p = parent_dir / "file.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=[]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is False

    def test_nonexistent_parent(self):
        p = pathlib.Path("/nonexistent/parent/file.flac")
        assert check_file_exists(p, case_sensitive=False) is False

    def test_subdirectory_file(self, parent_dir):
        (parent_dir / "sub").mkdir()
        p = parent_dir / "sub" / "song.flac"
        with (
            patch("tidaler.helper.path.os.listdir", return_value=["Song.flac"]),
            patch("tidaler.helper.path.os.path.isfile", return_value=True),
        ):
            assert check_file_exists(p, case_sensitive=False) is True
