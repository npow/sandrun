"""Micromamba binary management for sandrun.

Layer: Internal Utility
May only import from: stdlib

Handles auto-detection, download, and architecture validation of the
micromamba binary that is staged into sandbox environments for offline
conda package installation.
"""

from __future__ import annotations

import bz2
import io
import os
import shlex
import shutil
import tarfile
import tempfile
from pathlib import Path


_DEFAULT_CACHE_ROOT = os.path.join(Path.home(), ".cache", "sandrun", "micromamba")


def target_linux_arch() -> str | None:
    """Return the target Linux arch from env, or None if unset/unknown."""
    target = (os.environ.get("METAFLOW_SANDBOX_TARGET_PLATFORM") or "").lower()
    if "linux-aarch64" in target or "linux-arm64" in target:
        return "aarch64"
    if "linux-64" in target or "linux-x86_64" in target or "linux-amd64" in target:
        return "x86_64"
    return None


def target_micromamba_platform() -> str:
    """Return the micromamba platform string for the target architecture."""
    arch = target_linux_arch()
    if arch == "aarch64":
        return "linux-aarch64"
    return "linux-64"


def elf_arch(path: str) -> str | None:
    """Return the ELF machine arch of the binary at *path*, or None."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(20)
    except OSError:
        return None
    if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
        return None
    endianness = "<" if hdr[5] == 1 else ">" if hdr[5] == 2 else None
    if endianness is None:
        return None
    e_machine = int.from_bytes(hdr[18:20], byteorder="little" if endianness == "<" else "big")
    if e_machine == 62:
        return "x86_64"
    if e_machine == 183:
        return "aarch64"
    return "unknown"


def is_compatible_linux_micromamba(path: str) -> bool:
    """Return True if *path* is a Linux ELF for the target architecture."""
    target = target_linux_arch()
    binary_arch = elf_arch(path)
    if binary_arch is None:
        return False
    if target is None:
        return binary_arch in ("x86_64", "aarch64")
    return binary_arch == target


def auto_download_micromamba() -> str | None:
    """Download a compatible Linux micromamba binary and cache it locally.

    Controlled by env vars:
    - ``SANDRUN_STAGE_MICROMAMBA`` / ``METAFLOW_SANDBOX_STAGE_MICROMAMBA``:
      Set to ``0`` / ``false`` to disable auto-download.
    - ``SANDRUN_MICROMAMBA_PATH`` / ``METAFLOW_SANDBOX_MICROMAMBA_PATH``:
      Explicit path to a pre-downloaded binary (skips download).
    - ``SANDRUN_MICROMAMBA_CACHE_DIR`` / ``METAFLOW_SANDBOX_MICROMAMBA_CACHE_DIR``:
      Override the cache root directory.

    Returns the path to a compatible cached binary, or ``None`` if unavailable.
    Raises on network errors only when the binary is explicitly required.
    """
    # Honour the legacy env var name as well as the new one.
    cfg = (
        os.environ.get("SANDRUN_STAGE_MICROMAMBA")
        or os.environ.get("METAFLOW_SANDBOX_STAGE_MICROMAMBA", "")
    )
    if cfg and cfg.lower() in ("0", "false", "no", "off"):
        return None

    from urllib import request

    platform_id = target_micromamba_platform()
    cache_root = Path(
        os.environ.get("SANDRUN_MICROMAMBA_CACHE_DIR")
        or os.environ.get("METAFLOW_SANDBOX_MICROMAMBA_CACHE_DIR", _DEFAULT_CACHE_ROOT)
    )
    final_path = cache_root / platform_id / "micromamba"
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.is_file() and is_compatible_linux_micromamba(str(final_path)):
        return str(final_path)

    url = f"https://micro.mamba.pm/api/micromamba/{platform_id}/latest"
    with request.urlopen(url, timeout=30) as resp:
        payload = resp.read()

    tar_bytes = bz2.decompress(payload)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        member = tf.getmember("bin/micromamba")
        extracted = tf.extractfile(member)
        if extracted is None:
            raise RuntimeError("Failed to extract micromamba from download payload.")
        fd, tmp_name = tempfile.mkstemp(
            prefix="micromamba.",
            suffix=".tmp",
            dir=str(final_path.parent),
        )
        tmp_path = Path(tmp_name)
        import os as _os

        with _os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(extracted, out)

    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, final_path)
    return str(final_path)


def resolve_micromamba() -> tuple[str | None, bool]:
    """Locate or download a compatible Linux micromamba binary.

    Returns
    -------
    (path, staged)
        *path* is the local path to a compatible binary, or ``None``.
        *staged* is ``True`` if the binary should be staged into the sandbox.
    """
    explicit = os.environ.get("SANDRUN_MICROMAMBA_PATH") or os.environ.get(
        "METAFLOW_SANDBOX_MICROMAMBA_PATH"
    )
    if explicit:
        path = explicit
        compatible = is_compatible_linux_micromamba(path)
        return (path if compatible else None), compatible

    path = shutil.which("micromamba")
    if path and is_compatible_linux_micromamba(path):
        return path, True

    try:
        path = auto_download_micromamba()
        if path and is_compatible_linux_micromamba(path):
            return path, True
    except Exception:
        pass

    return None, False


# Remote path where micromamba is staged inside the sandbox.
STAGING_BIN_DIR = "/tmp/sandrun/bin"
REMOTE_MICROMAMBA_PATH = f"{STAGING_BIN_DIR}/micromamba"


def micromamba_stage_upload_spec(local_path: str) -> dict[str, str]:
    """Return the upload spec dict for staging micromamba into the sandbox."""
    return {
        "local": local_path,
        "remote": REMOTE_MICROMAMBA_PATH,
        "mode": "0755",
    }


def micromamba_path_export() -> str:
    """Bash snippet to prepend the staged micromamba bin dir to PATH."""
    return f"export PATH={shlex.quote(STAGING_BIN_DIR)}:$PATH"
