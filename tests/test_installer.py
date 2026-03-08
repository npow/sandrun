"""Unit tests for sandrun.installer."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from sandrun._types import PackageSpec
from sandrun.installer import _REMOTE_PKGS_DIR
from sandrun.installer import CondaOfflineInstaller
from sandrun.installer import NoopDepInstaller
from sandrun.installer import UvDepInstaller

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conda_spec(filename: str = "numpy-1.24.0.conda") -> PackageSpec:
    return PackageSpec(
        url=f"https://conda.anaconda.org/conda-forge/linux-64/{filename}",
        filename=filename,
        pkg_type="conda",
        hashes={"md5": "deadbeef"},
    )


def _pip_spec(filename: str = "requests-2.31.0-py3-none-any.whl") -> PackageSpec:
    return PackageSpec(
        url=f"https://files.pythonhosted.org/{filename}",
        filename=filename,
        pkg_type="pip",
    )


def _synthetic_spec() -> PackageSpec:
    return PackageSpec(
        url="file:///local/pkg.conda",
        filename="pkg.conda",
        pkg_type="conda",
        is_real_url=False,
    )


# ---------------------------------------------------------------------------
# NoopDepInstaller
# ---------------------------------------------------------------------------


class TestNoopDepInstaller:
    def test_prepare_is_noop(self) -> None:
        inst = NoopDepInstaller()
        inst.prepare([_conda_spec()], "linux-64")  # must not raise

    def test_stage_is_noop(self) -> None:
        inst = NoopDepInstaller()
        backend = MagicMock()
        inst.stage(backend, "sb-1")
        backend.upload.assert_not_called()

    def test_setup_commands_empty(self) -> None:
        assert NoopDepInstaller().setup_commands() == []

    def test_from_staged_returns_noop(self) -> None:
        inst = NoopDepInstaller.from_staged("/any/path")
        assert isinstance(inst, NoopDepInstaller)


# ---------------------------------------------------------------------------
# CondaOfflineInstaller — download logic is stubbed
# ---------------------------------------------------------------------------


class TestCondaOfflineInstallerPrepare:
    def test_skips_synthetic_packages(self, tmp_path: Path) -> None:
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        # Synthetic package — should not trigger any download.
        with patch.object(inst, "_download") as mock_dl:
            inst.prepare([_synthetic_spec()])
        mock_dl.assert_not_called()

    def test_writes_manifest(self, tmp_path: Path) -> None:
        pkg = _conda_spec()
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        with patch.object(CondaOfflineInstaller, "_download", side_effect=_fake_download):
            inst.prepare([pkg])

        manifest = tmp_path / CondaOfflineInstaller._MANIFEST_NAME
        assert manifest.exists()
        content = manifest.read_text()
        assert "conda\t" in content
        assert pkg.filename in content

    def test_from_staged_reads_manifest(self, tmp_path: Path) -> None:
        pkg = _conda_spec()
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        with patch.object(CondaOfflineInstaller, "_download", side_effect=_fake_download):
            inst.prepare([pkg])

        # Reconstruct from staged dir only.
        inst2 = CondaOfflineInstaller.from_staged(str(tmp_path))
        assert inst2._has_conda is True
        assert len(inst2._staged_files) == 1

    def test_from_staged_raises_if_no_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Staging manifest not found"):
            CondaOfflineInstaller.from_staged(str(tmp_path))

    def test_stage_uploads_each_file(self, tmp_path: Path) -> None:
        pkg = _conda_spec()
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        with patch.object(CondaOfflineInstaller, "_download", side_effect=_fake_download):
            inst.prepare([pkg])

        backend = MagicMock()
        backend.exec.return_value = MagicMock(exit_code=0)
        inst.stage(backend, "sb-1")

        backend.upload.assert_called_once()
        call_args = backend.upload.call_args
        assert call_args[0][0] == "sb-1"
        assert call_args[0][2] == f"{_REMOTE_PKGS_DIR}/{pkg.filename}"

    def test_setup_commands_include_micromamba_and_path(self, tmp_path: Path) -> None:
        pkg = _conda_spec()
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        with patch.object(CondaOfflineInstaller, "_download", side_effect=_fake_download):
            inst.prepare([pkg])

        cmds = inst.setup_commands()
        combined = " ".join(cmds)
        assert "micromamba create" in combined
        assert "--offline" in combined
        assert "export PATH=" in combined

    def test_empty_specs_produces_no_commands(self, tmp_path: Path) -> None:
        inst = CondaOfflineInstaller(staging_dir=str(tmp_path))
        inst.prepare([])
        assert inst.setup_commands() == []


# ---------------------------------------------------------------------------
# UvDepInstaller — subprocess is stubbed
# ---------------------------------------------------------------------------


class TestUvDepInstaller:
    def test_prepare_skips_conda_specs(self, tmp_path: Path) -> None:
        inst = UvDepInstaller(staging_dir=str(tmp_path))
        with patch("subprocess.run") as mock_run:
            inst.prepare([_conda_spec()], "linux-64")
        mock_run.assert_not_called()

    def test_prepare_calls_uv(self, tmp_path: Path) -> None:
        spec = _pip_spec()
        inst = UvDepInstaller(staging_dir=str(tmp_path))
        # Simulate uv download creating a wheel.
        whl = tmp_path / spec.filename
        whl.write_bytes(b"fake wheel")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            inst.prepare([spec], "linux-64")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "uv" in cmd
        assert "pip" in cmd
        assert "download" in cmd
        assert "linux_x86_64" in cmd

    def test_prepare_raises_if_uv_missing(self, tmp_path: Path) -> None:
        spec = _pip_spec()
        inst = UvDepInstaller(staging_dir=str(tmp_path))
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(RuntimeError, match="uv not found"),
        ):
            inst.prepare([spec], "linux-64")

    def test_from_staged_reads_manifest(self, tmp_path: Path) -> None:
        spec = _pip_spec()
        whl = tmp_path / spec.filename
        whl.write_bytes(b"x")
        manifest = tmp_path / UvDepInstaller._MANIFEST_NAME
        manifest.write_text(f"# header\n{spec.filename}\n")

        inst = UvDepInstaller.from_staged(str(tmp_path))
        assert spec.filename in inst._wheel_names

    def test_setup_commands_no_index(self, tmp_path: Path) -> None:
        spec = _pip_spec()
        inst = UvDepInstaller(staging_dir=str(tmp_path))
        inst._wheel_names = [spec.filename]

        cmds = inst.setup_commands()
        combined = " ".join(cmds)
        assert "--no-index" in combined
        assert "--find-links" in combined

    def test_setup_commands_empty_when_no_wheels(self, tmp_path: Path) -> None:
        inst = UvDepInstaller(staging_dir=str(tmp_path))
        assert inst.setup_commands() == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_download(url: str, dest: Path, hashes: dict) -> None:
    """Write fake bytes to dest without network access."""
    dest.write_bytes(b"fake package content")
