"""Abstract sandbox backend interface.

Layer: Core Abstraction
May only import from: stdlib, typing

Every concrete backend (Daytona, E2B, Modal, ...) MUST subclass
SandboxBackend and implement all abstract methods. Structural tests
in tests/structural/ enforce this at CI time.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import Callable


@dataclass(frozen=True)
class Resources:
    """Resource requests for a sandbox."""

    cpu: int = 1
    memory_mb: int = 1024
    gpu: str | None = None


@dataclass(frozen=True)
class SandboxConfig:
    """Everything needed to create a sandbox."""

    image: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    resources: Resources = field(default_factory=Resources)
    timeout: int = 300  # seconds


@dataclass(frozen=True)
class ExecResult:
    """Result of executing a command in a sandbox."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxBackend(ABC):
    """Interface that every sandbox provider must implement.

    Methods are deliberately simple — create, run a command,
    move files, tear down. Backends handle provider-specific
    details (auth, SDK quirks, reconnection) internally.
    """

    @abstractmethod
    def create(self, config: SandboxConfig) -> str:
        """Create a sandbox and return its ID."""

    @abstractmethod
    def exec(
        self,
        sandbox_id: str,
        cmd: list[str],
        cwd: str = "/",
        timeout: int = 300,
    ) -> ExecResult:
        """Run a shell command inside the sandbox."""

    @abstractmethod
    def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        """Copy a local file into the sandbox."""

    @abstractmethod
    def download(self, sandbox_id: str, remote_path: str, local_path: str) -> None:
        """Copy a file from the sandbox to a local path."""

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None:
        """Tear down the sandbox and release resources."""

    def exec_script(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
    ) -> ExecResult:
        """Run a multi-line bash script inside the sandbox.

        Default implementation delegates to :meth:`exec`. Backends may
        override for more efficient execution (e.g. writing to a temp
        file and sourcing it).
        """
        return self.exec(sandbox_id, ["bash", "-c", script], timeout=timeout)

    def exec_script_streaming(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Run a bash script with optional streaming log callbacks.

        Default: blocking exec_script, then call callbacks with the full
        output at the end.  Backends that support live log streaming
        (e.g. Daytona via session-based async execution) should override
        this to emit lines incrementally while the script runs.

        ``on_stdout`` / ``on_stderr`` are called once per line (no newline).
        """
        result = self.exec_script(sandbox_id, script, timeout)
        if on_stdout and result.stdout:
            for line in result.stdout.splitlines():
                on_stdout(line)
        if on_stderr and result.stderr:
            for line in result.stderr.splitlines():
                on_stderr(line)
        return result
