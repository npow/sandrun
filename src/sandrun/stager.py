"""Code package staging — delivers source code into a sandbox.

Layer: Staging Abstraction
May only import from: sandrun.backend, stdlib, typing

PackageStager: ABC for staging a code package into a sandbox via the
backend filesystem API (no S3 or other object storage required).

TarballStager: Concrete impl — stages a .tar archive from local disk,
uploads it to the sandbox, and returns shell commands to extract it.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sandrun.backend import SandboxBackend

# Remote paths used inside the sandbox.
_REMOTE_CODE_ARCHIVE = "/tmp/.sandrun-code.tar"
_REMOTE_WORK_DIR = "/tmp/.sandrun-workdir"


class PackageStager(ABC):
    """Delivers a code package into a sandbox via the backend filesystem.

    Two-phase protocol
    ------------------
    1. **Host phase** (before sandbox creation): package source is available
       locally — write it to a temp file.  Constructors handle this.
    2. **Deliver phase** (after sandbox creation): call :meth:`deliver` to
       upload the package, then :meth:`setup_commands` to obtain the shell
       commands that extract / prepare it inside the sandbox.
    """

    @abstractmethod
    def deliver(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        """Upload the staged package to the sandbox."""

    @abstractmethod
    def setup_commands(self) -> list[str]:
        """Shell commands to extract/prepare the package inside the sandbox.

        These commands are prepended to the user script before execution.
        Each command must be idempotent (safe to re-run on infra retry).
        The final command MUST leave the working directory set to wherever
        the entry-point script (flow.py or user script) is located.
        """


class TarballStager(PackageStager):
    """Uploads a .tar archive to the sandbox and extracts it in-place.

    The archive is uploaded to ``/tmp/.sandrun-code.tar`` and extracted
    into ``/tmp/.sandrun-workdir``.  The setup commands leave the CWD
    set to the extracted directory so the user script is runnable with
    ``python script.py``.

    Parameters
    ----------
    local_path:
        Path to the .tar file on the host machine.  The file must exist
        for the lifetime of the :class:`SandboxRunner` that uses this stager.
    """

    def __init__(self, local_path: str) -> None:
        if not os.path.isfile(local_path):
            raise FileNotFoundError(
                f"TarballStager: local_path does not exist: {local_path!r}"
            )
        self._local_path = local_path

    @classmethod
    def from_bytes(cls, data: bytes) -> "TarballStager":
        """Write *data* to a temp file and return a :class:`TarballStager`.

        The temp file is created with a `.tar` suffix in the system temp
        directory.  The caller is responsible for deleting it when done,
        or simply letting the OS clean it up.
        """
        fd, path = tempfile.mkstemp(suffix=".tar", prefix="sandrun-code-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return cls(path)

    @property
    def local_path(self) -> str:
        """Local path to the staged tarball."""
        return self._local_path

    def deliver(self, backend: "SandboxBackend", sandbox_id: str) -> None:
        """Upload the tarball to the sandbox at ``_REMOTE_CODE_ARCHIVE``."""
        backend.upload(sandbox_id, self._local_path, _REMOTE_CODE_ARCHIVE)

    def setup_commands(self) -> list[str]:
        """Return commands to extract the archive and cd into the work dir."""
        remote = shlex.quote(_REMOTE_CODE_ARCHIVE)
        work_dir = shlex.quote(_REMOTE_WORK_DIR)
        return [
            f"mkdir -p {work_dir} && cd {work_dir} && tar xf {remote}",
        ]

    def cleanup(self) -> None:
        """Delete the local temp file.  Safe to call multiple times."""
        try:
            Path(self._local_path).unlink(missing_ok=True)
        except OSError:
            pass
