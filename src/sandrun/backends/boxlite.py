"""Boxlite sandbox backend.

Layer: Concrete Backend
May only import from: sandrun.backend (ABC + dataclasses), boxlite SDK

Install: pip install sandrun[boxlite]
Docs:    https://github.com/boxlite-ai/boxlite

Boxlite runs steps in local microVMs with hardware isolation (KVM on Linux,
HVF on macOS). No cloud account or API key is required.

Each sandbox gets its own SyncSimpleBox with an isolated boxlite runtime.
This avoids greenlet bridge contention between parallel foreach branches —
N runtimes for N VMs, all lightweight and independent.

Limitation: boxes are local only. Sandbox IDs cannot be reconnected after
the Python process exits, and boxes cannot run on a remote machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import uuid
from pathlib import Path
from typing import Callable

from sandrun.backend import ExecResult
from sandrun.backend import SandboxBackend
from sandrun.backend import SandboxConfig

_INSTALL_HINT = (
    "Boxlite SDK not found. Install it with:\n"
    "\n"
    "    pip install sandrun[boxlite]\n"
    "\n"
    "Boxlite requires KVM (Linux) or HVF (macOS) for hardware isolation.\n"
    "No API key needed — runs entirely on your machine.\n"
    "See https://github.com/boxlite-ai/boxlite for details."
)


def _get_sync_simplebox():  # type: ignore[no-untyped-def]
    try:
        from boxlite.sync_api import SyncSimpleBox
    except ImportError:
        raise ImportError(_INSTALL_HINT) from None
    return SyncSimpleBox


class BoxliteBackend(SandboxBackend):
    """Runs tasks in local boxlite microVMs (KVM/HVF hardware isolation)."""

    def __init__(self) -> None:
        _get_sync_simplebox()  # Fail fast if SDK not installed.
        self._boxes: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def create(self, config: SandboxConfig) -> str:
        SyncSimpleBox = _get_sync_simplebox()
        # BoxOptions.env is List[Tuple[str, str]]; SandboxConfig.env is dict.
        env_list = list(config.env.items()) if config.env else []
        box = SyncSimpleBox(
            image=config.image or "python:slim",
            memory_mib=config.resources.memory_mb,
            cpus=config.resources.cpu,
            env=env_list,
            auto_remove=True,
        )
        box.__enter__()
        self._boxes[box.id] = box
        return box.id

    def destroy(self, sandbox_id: str) -> None:
        box = self._boxes.pop(sandbox_id, None)
        if box is not None:
            with contextlib.suppress(Exception):
                box.__exit__(None, None, None)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def exec(
        self,
        sandbox_id: str,
        cmd: list[str],
        cwd: str = "/",
        timeout: int = 300,
    ) -> ExecResult:
        box = self._get_box(sandbox_id)
        command = shlex.join(cmd)
        if cwd and cwd != "/":
            command = f"cd {shlex.quote(cwd)} && {command}"
        # SyncSimpleBox.exec() collects stdout/stderr concurrently via
        # asyncio.gather internally, so no deadlock risk from pipe buffers.
        result = box.exec("bash", "-c", command)
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def exec_script(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
    ) -> ExecResult:
        return self.exec_script_streaming(sandbox_id, script, timeout)

    def exec_script_streaming(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Run a bash script with optional real-time log streaming.

        Writes the script to /root/ (not /tmp/): boxlite's copy_in writes to
        the rootfs layer and silently fails for tmpfs mounts like /tmp inside
        the guest. We write via exec+base64 to avoid that entirely.

        When callbacks are supplied, streams stdout/stderr line-by-line using
        a single asyncio.gather coroutine through the greenlet bridge —
        the same deadlock-free pattern SyncSimpleBox.exec() uses internally.
        """
        box = self._get_box(sandbox_id)
        remote_script = f"/root/sandrun-{uuid.uuid4().hex}.sh"

        # Write script via exec to sidestep the tmpfs copy_in limitation.
        import base64

        b64 = base64.b64encode(script.encode()).decode()
        setup = box.exec(
            "bash",
            "-c",
            f"echo {shlex.quote(b64)} | base64 -d > {remote_script} && chmod 700 {remote_script}",
        )
        if setup.exit_code != 0:
            return ExecResult(
                exit_code=setup.exit_code,
                stdout=setup.stdout or "",
                stderr=setup.stderr or f"Failed to write script to {remote_script}",
            )

        command = f"bash {remote_script}"

        try:
            if on_stdout is None and on_stderr is None:
                result = box.exec("bash", "-c", command)
                return ExecResult(
                    exit_code=result.exit_code,
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                )

            # Streaming: drive the async execution through the greenlet bridge
            # as a single coroutine. asyncio.gather reads both streams
            # concurrently to prevent pipe-buffer deadlock.
            #
            # Access path: SyncSimpleBox._box → SyncBox; SyncBox._box → async Box (Rust)
            async_box = box._box._box  # type: ignore[union-attr]

            async def _run_streaming() -> ExecResult:
                execution = await async_box.exec("bash", ["-c", command])

                stdout_parts: list[str] = []
                stderr_parts: list[str] = []

                async def _read(
                    stream_fn,
                    parts: list[str],
                    cb: Callable[[str], None] | None,
                ) -> None:
                    stream = stream_fn()
                    if stream is None:
                        return
                    buf = ""
                    async for chunk in stream:
                        if isinstance(chunk, bytes):
                            chunk = chunk.decode("utf-8", errors="replace")
                        buf += chunk
                        lines = buf.split("\n")
                        for line in lines[:-1]:
                            parts.append(line)
                            if cb:
                                cb(line)
                        buf = lines[-1]
                    if buf:
                        parts.append(buf)
                        if cb:
                            cb(buf)

                await asyncio.gather(
                    _read(execution.stdout, stdout_parts, on_stdout),
                    _read(execution.stderr, stderr_parts, on_stderr),
                )

                exec_result = await execution.wait()
                return ExecResult(
                    exit_code=exec_result.exit_code,
                    stdout="\n".join(stdout_parts),
                    stderr="\n".join(stderr_parts),
                )

            return box._runtime._sync(_run_streaming())  # type: ignore[union-attr]

        finally:
            with contextlib.suppress(Exception):
                box.exec("bash", "-c", f"rm -f {shlex.quote(remote_script)}")

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        """Copy a local file into the sandbox.

        Note: boxlite copy_in writes to the rootfs layer. Paths that are
        tmpfs mounts in the guest (e.g. /tmp, /dev/shm) will silently fail —
        use /root/ or /var/tmp/ for reliable delivery.
        """
        box = self._get_box(sandbox_id)
        _copy_in(box, local_path, remote_path)

    def download(self, sandbox_id: str, remote_path: str, local_path: str) -> None:
        box = self._get_box(sandbox_id)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        _copy_out(box, remote_path, local_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_box(self, sandbox_id: str) -> object:
        box = self._boxes.get(sandbox_id)
        if box is None:
            raise KeyError(
                f"Boxlite sandbox '{sandbox_id}' not found. "
                "Boxlite is local-only — box handles do not survive process restarts."
            )
        return box


def _copy_in(box: object, host_path: str, container_dest: str) -> None:
    """Run copy_in through the greenlet bridge (SyncSimpleBox has no copy_in)."""
    from boxlite import CopyOptions  # lazy import — only when boxlite is installed

    opts = CopyOptions(
        recursive=True,
        overwrite=True,
        follow_symlinks=False,
        include_parent=True,
    )
    # SyncSimpleBox._box = SyncBox; SyncBox._box = async Box (Rust-backed)
    async_box = box._box._box  # type: ignore[union-attr]
    box._runtime._sync(async_box.copy_in(host_path, container_dest, opts))  # type: ignore[union-attr]


def _copy_out(box: object, container_src: str, host_dest: str) -> None:
    """Run copy_out through the greenlet bridge (SyncSimpleBox has no copy_out)."""
    from boxlite import CopyOptions  # lazy import — only when boxlite is installed

    opts = CopyOptions(
        recursive=True,
        overwrite=True,
        follow_symlinks=False,
        include_parent=True,
    )
    async_box = box._box._box  # type: ignore[union-attr]
    box._runtime._sync(async_box.copy_out(container_src, host_dest, opts))  # type: ignore[union-attr]
