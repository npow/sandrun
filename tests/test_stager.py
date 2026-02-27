"""Unit tests for sandrun.stager."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sandrun.stager import _REMOTE_CODE_ARCHIVE
from sandrun.stager import _REMOTE_WORK_DIR
from sandrun.stager import TarballStager


class TestTarballStager:
    def test_from_existing_file(self, tmp_path: Path) -> None:
        tar = tmp_path / "code.tar"
        tar.write_bytes(b"fake tar content")
        stager = TarballStager(str(tar))
        assert stager.local_path == str(tar)

    def test_rejects_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            TarballStager("/nonexistent/path/code.tar")

    def test_from_bytes_writes_temp_file(self) -> None:
        data = b"\x1f\x8b fake tarball"
        stager = TarballStager.from_bytes(data)
        try:
            assert os.path.isfile(stager.local_path)
            assert Path(stager.local_path).read_bytes() == data
            assert stager.local_path.endswith(".tar")
        finally:
            stager.cleanup()

    def test_cleanup_removes_file(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        stager = TarballStager(path)
        stager.cleanup()
        assert not os.path.exists(path)

    def test_cleanup_is_idempotent(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        stager = TarballStager(path)
        stager.cleanup()
        stager.cleanup()  # second call must not raise

    def test_deliver_calls_backend_upload(self, tmp_path: Path) -> None:
        tar = tmp_path / "code.tar"
        tar.write_bytes(b"x")
        stager = TarballStager(str(tar))

        backend = MagicMock()
        stager.deliver(backend, "sb-1")

        backend.upload.assert_called_once_with("sb-1", str(tar), _REMOTE_CODE_ARCHIVE)

    def test_setup_commands_extract_and_cd(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        stager = TarballStager(path)
        cmds = stager.setup_commands()

        assert len(cmds) == 1
        cmd = cmds[0]
        assert "mkdir -p" in cmd
        assert "tar xf" in cmd
        assert _REMOTE_WORK_DIR in cmd
        assert _REMOTE_CODE_ARCHIVE in cmd
        stager.cleanup()
