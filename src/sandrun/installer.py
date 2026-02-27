"""Dependency installation via backend filesystem (no-egress safe).

Layer: Installation Abstraction
May only import from: sandrun.backend, sandrun._types, sandrun._micromamba, stdlib

Three-phase protocol
--------------------
1. prepare(specs, target_arch)  — host, has internet — downloads packages
2. stage(backend, sandbox_id)   — host, sandbox running — uploads via backend.upload()
3. setup_commands()             — returns shell cmds run inside the sandbox

This design ensures sandboxes with no outbound network access can still
install dependencies: all packages are fetched on the host and delivered
via the provider's filesystem API.

Implementations
---------------
NoopDepInstaller     — no-op, use when image already has all deps
CondaOfflineInstaller — micromamba offline install (conda + pip packages)
UvDepInstaller        — uv pip offline install (pip-only packages)
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from sandrun._micromamba import REMOTE_MICROMAMBA_PATH
from sandrun._micromamba import STAGING_BIN_DIR
from sandrun._micromamba import micromamba_path_export
from sandrun._types import PackageSpec

if TYPE_CHECKING:
    from sandrun.backend import SandboxBackend

# Remote path where packages are staged inside the sandbox.
_REMOTE_PKGS_DIR = "/tmp/.sandrun-pkgs"

# Remote conda env prefix (micromamba installs here).
_REMOTE_ENV_DIR = "/opt/sandrun-env"

# Cache root for downloaded packages on the host.
_DEFAULT_CACHE_ROOT = os.path.join(Path.home(), ".cache", "sandrun", "packages")


class DepInstaller(ABC):
    """Three-phase dependency installer for no-egress sandboxes.

    Subclasses implement :meth:`prepare`, :meth:`stage`, and
    :meth:`setup_commands`.  The orchestrator (SandboxRunner or
    SandboxExecutor) calls them in order:

    .. code-block:: python

        installer.prepare(specs, target_arch="linux-64")   # host, before create()
        sandbox_id = backend.create(config)
        installer.stage(backend, sandbox_id)               # host, after create()
        # ...run script that includes installer.setup_commands()...
    """

    @abstractmethod
    def prepare(
        self,
        specs: list[PackageSpec],
        target_arch: str = "linux-64",
    ) -> None:
        """Download packages to a local staging directory.

        Parameters
        ----------
        specs:
            Pinned package specifications.  Only specs with
            ``is_real_url=True`` are downloaded; others are skipped.
        target_arch:
            Target platform string for cross-arch downloads, e.g.
            ``"linux-64"`` or ``"linux-aarch64"``.
        """

    @abstractmethod
    def stage(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        """Upload the staged packages to the sandbox.

        Must be called after :meth:`prepare` and after the sandbox is created.
        """

    @abstractmethod
    def setup_commands(self) -> list[str]:
        """Return shell commands to install packages offline inside the sandbox.

        Commands are inserted into the sandbox script before the user command.
        Must not require network access.
        """

    @classmethod
    def from_staged(cls, staging_dir: str) -> "DepInstaller":  # type: ignore[return]
        """Reconstruct an installer from an already-prepared staging directory.

        Used when the host prepared deps before launching the sandbox (e.g.
        metaflow_sandbox decorator), and the executor receives only the path.

        Raises NotImplementedError by default; subclasses override.
        """
        raise NotImplementedError(f"{cls.__name__} does not support from_staged()")


class NoopDepInstaller(DepInstaller):
    """No-op installer — use when the sandbox image already has all deps."""

    def prepare(self, specs: list[PackageSpec], target_arch: str = "linux-64") -> None:
        pass

    def stage(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        pass

    def setup_commands(self) -> list[str]:
        return []

    @classmethod
    def from_staged(cls, staging_dir: str) -> "NoopDepInstaller":
        return cls()


class CondaOfflineInstaller(DepInstaller):
    """Three-phase offline installer for conda (and pip) packages.

    Downloads conda ``.conda`` / ``.tar.bz2`` packages and pip ``.whl``
    files to a local cache directory, uploads them to the sandbox, then
    runs ``micromamba create --offline`` inside the sandbox.

    The micromamba binary must already be staged in the sandbox at
    ``REMOTE_MICROMAMBA_PATH`` (handled by :class:`~sandrun.runner.SandboxRunner`
    when micromamba staging is enabled).

    Parameters
    ----------
    staging_dir:
        Local directory for downloaded packages.  Created automatically
        if not provided.  Pass an existing directory to reuse a prior
        :meth:`prepare` result (e.g. after deserialising from CLI).
    cache_dir:
        Local directory for the persistent package cache.  Packages are
        named ``<sha256_of_url>.ext`` so the cache is content-addressed.
        Defaults to ``~/.cache/sandrun/packages``.
    """

    _MANIFEST_NAME = "sandrun-manifest.txt"

    def __init__(
        self,
        staging_dir: str | None = None,
        cache_dir: str | None = None,
    ) -> None:
        if staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="sandrun-deps-")
            self._owns_staging_dir = True
        else:
            self._staging_dir = staging_dir
            self._owns_staging_dir = False

        self._cache_dir = Path(
            cache_dir
            or os.environ.get("SANDRUN_PACKAGE_CACHE_DIR", _DEFAULT_CACHE_ROOT)
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Populated by prepare(); consumed by stage().
        self._staged_files: list[tuple[str, str]] = []  # (local, remote)
        self._has_conda = False
        self._has_pip = False
        self._pip_packages: list[str] = []

    @classmethod
    def from_staged(cls, staging_dir: str) -> "CondaOfflineInstaller":
        """Reconstruct from an already-prepared *staging_dir*.

        Reads the manifest written by :meth:`prepare` to rebuild the
        file list without re-downloading.
        """
        inst = cls(staging_dir=staging_dir)
        manifest = Path(staging_dir) / cls._MANIFEST_NAME
        if not manifest.exists():
            raise FileNotFoundError(
                f"Staging manifest not found: {manifest}. "
                "Was CondaOfflineInstaller.prepare() called on this directory?"
            )
        for line in manifest.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, local, remote = line.split("\t", 2)
            inst._staged_files.append((local, remote))
            if kind == "conda":
                inst._has_conda = True
            elif kind == "pip":
                inst._has_pip = True
                inst._pip_packages.append(Path(local).stem.split("-")[0])
        return inst

    def prepare(
        self,
        specs: list[PackageSpec],
        target_arch: str = "linux-64",
    ) -> None:
        """Download packages to the local staging directory.

        Each package is first looked up in the content-addressed local
        cache; only missing packages are downloaded from their URLs.
        A manifest file is written so :meth:`from_staged` can reconstruct
        the installer from just the staging directory path.
        """
        staging = Path(self._staging_dir)
        manifest_lines: list[str] = ["# sandrun dep manifest — do not edit"]

        for spec in specs:
            if not spec.is_real_url:
                continue

            # Use a content-addressed cache key based on the URL.
            url_hash = hashlib.sha256(spec.url.encode()).hexdigest()[:16]
            ext = spec.url_format or Path(spec.filename).suffix
            cache_key = f"{url_hash}{ext}"
            cache_path = self._cache_dir / cache_key

            if not cache_path.exists():
                self._download(spec.url, cache_path, spec.hashes)

            # Copy/hard-link from cache into staging dir.
            dest = staging / spec.filename
            if not dest.exists():
                try:
                    os.link(cache_path, dest)
                except OSError:
                    import shutil

                    shutil.copy2(cache_path, dest)

            remote = f"{_REMOTE_PKGS_DIR}/{spec.filename}"
            self._staged_files.append((str(dest), remote))

            kind = spec.pkg_type  # "conda" or "pip"
            manifest_lines.append(f"{kind}\t{dest}\t{remote}")

            if kind == "conda":
                self._has_conda = True
            elif kind == "pip":
                self._has_pip = True
                self._pip_packages.append(spec.filename)

        (staging / self._MANIFEST_NAME).write_text("\n".join(manifest_lines) + "\n")

    @staticmethod
    def _download(url: str, dest: Path, hashes: dict[str, str]) -> None:
        """Download *url* to *dest*, verifying *hashes* if provided."""
        from urllib import request

        tmp = dest.with_suffix(".tmp")
        try:
            with request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            if hashes:
                for alg, expected in hashes.items():
                    h = hashlib.new(alg, data).hexdigest()
                    if h != expected:
                        raise ValueError(
                            f"Hash mismatch for {url}: "
                            f"{alg}={h!r}, expected {expected!r}"
                        )
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def stage(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        """Upload all staged packages to the sandbox."""
        if not self._staged_files:
            return
        # Create remote pkgs dir; ignore errors (dir may exist from prior upload).
        backend.exec(sandbox_id, ["mkdir", "-p", _REMOTE_PKGS_DIR])
        for local, remote in self._staged_files:
            backend.upload(sandbox_id, local, remote)

    def setup_commands(self) -> list[str]:
        """Return commands to install packages offline inside the sandbox.

        Emits:
        1. A ``micromamba create --offline`` call (if any conda packages).
        2. A ``pip install --no-index`` call (if any pip-only packages remain).
        3. ``export PATH=...`` to activate the created env.
        """
        if not self._staged_files:
            return []

        cmds: list[str] = [micromamba_path_export()]

        if self._has_conda:
            env_dir = shlex.quote(_REMOTE_ENV_DIR)
            pkgs_dir = shlex.quote(_REMOTE_PKGS_DIR)
            cmds.append(
                f"micromamba create --offline -y -q"
                f" --no-rc -p {env_dir}"
                f" -c {pkgs_dir} --override-channels"
            )
            cmds.append(f"export PATH={_REMOTE_ENV_DIR}/bin:$PATH")

        if self._has_pip and not self._has_conda:
            # Pip-only path: install into whatever Python is on PATH.
            pkgs_dir = shlex.quote(_REMOTE_PKGS_DIR)
            cmds.append(
                f"pip install --no-index --find-links {pkgs_dir} "
                + " ".join(shlex.quote(p) for p in self._pip_packages)
            )

        return cmds


class UvDepInstaller(DepInstaller):
    """Three-phase offline pip installer using ``uv``.

    Uses ``uv pip download`` to fetch wheels for the target platform on
    the host, uploads them to the sandbox, then runs
    ``pip install --no-index --find-links`` inside.

    Requires ``uv`` to be installed on the host.  See
    https://docs.astral.sh/uv/ for installation instructions.

    Parameters
    ----------
    staging_dir:
        Local directory for downloaded wheels.  Created automatically
        if not provided.
    """

    _MANIFEST_NAME = "sandrun-uv-manifest.txt"

    def __init__(self, staging_dir: str | None = None) -> None:
        if staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="sandrun-uv-deps-")
            self._owns_staging_dir = True
        else:
            self._staging_dir = staging_dir
            self._owns_staging_dir = False

        self._wheel_names: list[str] = []

    @classmethod
    def from_pep723(cls, script_path: str) -> "UvDepInstaller":
        """Create an installer by reading PEP 723 inline script metadata.

        The script must contain a ``# /// script`` block with
        ``[tool.uv]`` or ``[project]`` TOML specifying dependencies.
        Resolution is delegated to ``uv``; the result is exact pinned
        wheel URLs for the target platform.
        """
        # Defer import so this module doesn't require uv at import time.
        return cls._from_pep723_path(script_path)

    @classmethod
    def _from_pep723_path(cls, script_path: str) -> "UvDepInstaller":
        inst = cls()
        inst._script_path = script_path
        return inst

    @classmethod
    def from_staged(cls, staging_dir: str) -> "UvDepInstaller":
        """Reconstruct from an already-prepared *staging_dir*."""
        inst = cls(staging_dir=staging_dir)
        manifest = Path(staging_dir) / cls._MANIFEST_NAME
        if not manifest.exists():
            raise FileNotFoundError(
                f"Staging manifest not found: {manifest}. "
                "Was UvDepInstaller.prepare() called on this directory?"
            )
        for line in manifest.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            inst._wheel_names.append(line)
        return inst

    def prepare(
        self,
        specs: list[PackageSpec],
        target_arch: str = "linux-64",
    ) -> None:
        """Download wheels for each spec using ``uv pip download``.

        *specs* with ``pkg_type="conda"`` are silently ignored — this
        installer handles pip-only environments.
        """
        # Map target arch to uv's platform tag.
        platform_map = {
            "linux-64": "linux_x86_64",
            "linux-aarch64": "linux_aarch64",
            "linux-arm64": "linux_aarch64",
        }
        uv_platform = platform_map.get(target_arch, "linux_x86_64")

        pip_specs = [s for s in specs if s.pkg_type == "pip" and s.is_real_url]
        if not pip_specs:
            return

        urls = [s.url for s in pip_specs]
        cmd = [
            "uv",
            "pip",
            "download",
            "--platform",
            uv_platform,
            "--no-deps",
            "-d",
            self._staging_dir,
            *urls,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            raise RuntimeError(
                "uv not found. Install it with:\n"
                "\n"
                "    pip install uv\n"
                "\n"
                "or visit https://docs.astral.sh/uv/ for options."
            ) from None

        manifest_lines = ["# sandrun uv manifest — do not edit"]
        for path in Path(self._staging_dir).glob("*.whl"):
            self._wheel_names.append(path.name)
            manifest_lines.append(path.name)

        (Path(self._staging_dir) / self._MANIFEST_NAME).write_text(
            "\n".join(manifest_lines) + "\n"
        )

    def stage(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        """Upload all downloaded wheels to the sandbox."""
        if not self._wheel_names:
            return
        backend.exec(sandbox_id, ["mkdir", "-p", _REMOTE_PKGS_DIR])
        for name in self._wheel_names:
            local = os.path.join(self._staging_dir, name)
            remote = f"{_REMOTE_PKGS_DIR}/{name}"
            backend.upload(sandbox_id, local, remote)

    def setup_commands(self) -> list[str]:
        """Return ``pip install --no-index --find-links ...`` command."""
        if not self._wheel_names:
            return []
        pkgs_dir = shlex.quote(_REMOTE_PKGS_DIR)
        packages = " ".join(shlex.quote(w.split("-")[0]) for w in self._wheel_names)
        return [
            f"pip install --no-index --find-links {pkgs_dir} {packages}",
        ]
