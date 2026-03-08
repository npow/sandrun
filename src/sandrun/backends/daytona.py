"""Daytona sandbox backend.

Layer: Concrete Backend
May only import from: sandrun.backend (ABC + dataclasses), daytona SDK

Install: pip install sandrun[daytona]
Docs:    https://www.daytona.io/docs/en/python-sdk/
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import time
import uuid
from math import ceil
from pathlib import Path
from typing import Callable

from sandrun.backend import ExecResult
from sandrun.backend import SandboxBackend
from sandrun.backend import SandboxConfig

# How often (seconds) to poll for new log lines.  Tune with
# METAFLOW_SANDBOX_LOG_POLL_INTERVAL (float, default 2.0).
_DEFAULT_POLL_INTERVAL = 2.0

_INSTALL_HINT = (
    "Daytona SDK not found. Install it with:\n"
    "\n"
    "    pip install sandrun[daytona]\n"
    "\n"
    "Then set DAYTONA_API_KEY and (optionally) DAYTONA_API_URL.\n"
    "See https://www.daytona.io/docs/ for details."
)


def _get_client():  # type: ignore[no-untyped-def]
    try:
        from daytona import Daytona
    except ImportError:
        raise ImportError(_INSTALL_HINT) from None
    return Daytona()


class DaytonaBackend(SandboxBackend):
    """Runs tasks in Daytona sandboxes (<100ms cold start)."""

    def __init__(self) -> None:
        self._client = _get_client()
        self._sandboxes: dict[str, object] = {}

    @staticmethod
    def _response_debug_blob(response) -> str:  # type: ignore[no-untyped-def]
        if hasattr(response, "model_dump"):
            try:
                return json.dumps(response.model_dump(), default=str)
            except Exception:
                return str(response.model_dump())
        return str(response)

    @staticmethod
    def _normalize_exec_response(response) -> ExecResult:  # type: ignore[no-untyped-def]
        exit_code = getattr(response, "exit_code", -1)
        result = getattr(response, "result", "") or ""
        stdout = result
        stderr = ""
        if hasattr(response, "model_dump"):
            dumped = response.model_dump()
            artifacts = dumped.get("artifacts") or {}
            if not stdout:
                stdout = artifacts.get("stdout") or ""
            stderr = artifacts.get("stderr") or ""
            if not stderr and dumped.get("additional_properties"):
                stderr = str(dumped.get("additional_properties"))
        if exit_code == -1:
            debug_blob = DaytonaBackend._response_debug_blob(response)
            stderr = f"{stderr}\n[daytona-debug] {debug_blob}".strip()
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def create(self, config: SandboxConfig) -> str:
        from daytona import CreateSandboxFromImageParams
        from daytona import Resources

        gpu_count = None
        if config.resources.gpu:
            try:
                gpu_count = int(config.resources.gpu)
            except ValueError:
                gpu_count = None

        # Metaflow uses memory in MB; Daytona resources.memory expects GB.
        memory_gb = max(1, ceil(config.resources.memory_mb / 1024))
        resources = Resources(
            cpu=config.resources.cpu,
            memory=memory_gb,
            gpu=gpu_count,
        )

        params = CreateSandboxFromImageParams(
            image=config.image or "python:3.11-slim",
            env_vars=config.env or None,
            auto_stop_interval=max(1, config.timeout // 60),
            resources=resources,
        )
        sandbox = self._client.create(params)
        sandbox_id = sandbox.id
        self._sandboxes[sandbox_id] = sandbox
        return sandbox_id

    def _get_sandbox(self, sandbox_id: str) -> object:
        if sandbox_id not in self._sandboxes:
            self._sandboxes[sandbox_id] = self._client.get(sandbox_id)
        return self._sandboxes[sandbox_id]

    def exec(
        self,
        sandbox_id: str,
        cmd: list[str],
        cwd: str = "/",
        timeout: int = 300,
    ) -> ExecResult:
        sandbox = self._get_sandbox(sandbox_id)
        command_str = shlex.join(cmd)
        for attempt in range(3):
            response = sandbox.process.exec(
                f"bash -lc {shlex.quote(command_str)}", cwd=cwd, timeout=timeout
            )
            result = self._normalize_exec_response(response)
            if result.exit_code != -1:
                return result
            if attempt < 2:
                time.sleep(2**attempt)
        return result

    def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        sandbox = self._get_sandbox(sandbox_id)
        content = Path(local_path).read_bytes()
        sandbox.fs.upload_file(content, remote_path)

    def download(self, sandbox_id: str, remote_path: str, local_path: str) -> None:
        sandbox = self._get_sandbox(sandbox_id)
        content = sandbox.fs.download_file(remote_path)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    def exec_script(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
    ) -> ExecResult:
        """Run a bash script directly via Daytona's shell execution.

        Metaflow generates a bash script (uses bash-specific redirections),
        so execute under ``bash -lc`` explicitly.
        """
        sandbox = self._get_sandbox(sandbox_id)
        remote_script = f"/tmp/sandrun-{uuid.uuid4().hex}.sh"
        sandbox.fs.upload_file(script.encode("utf-8"), remote_script)
        command = f"bash -lc {shlex.quote(f'chmod 700 {remote_script} && bash {remote_script}')}"
        try:
            for attempt in range(3):
                response = sandbox.process.exec(command, timeout=timeout)
                result = self._normalize_exec_response(response)
                if result.exit_code != -1:
                    return result
                if attempt < 2:
                    time.sleep(2**attempt)
            return result
        finally:
            # Best effort cleanup.
            sandbox.process.exec(
                f"bash -lc {shlex.quote(f'rm -f {remote_script}')}",
                timeout=min(timeout, 30),
            )

    def exec_script_streaming(
        self,
        sandbox_id: str,
        script: str,
        timeout: int = 600,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Run the step script via a Daytona session with incremental log polling.

        Uses ``process.execute_session_command(run_async=True)`` so the
        script starts immediately while we poll ``get_session_command_logs``
        every ``METAFLOW_SANDBOX_LOG_POLL_INTERVAL`` seconds, emitting new
        lines through the callbacks in near real-time.

        Falls back to the blocking ``exec_script`` path if no callbacks are
        requested (e.g. when streaming is disabled by the caller).
        """
        if on_stdout is None and on_stderr is None:
            return self.exec_script(sandbox_id, script, timeout)

        try:
            from daytona import SessionExecuteRequest
        except ImportError:
            raise ImportError(_INSTALL_HINT) from None

        poll_interval = float(
            os.environ.get("METAFLOW_SANDBOX_LOG_POLL_INTERVAL", str(_DEFAULT_POLL_INTERVAL))
        )

        sandbox = self._get_sandbox(sandbox_id)
        session_id = f"sandrun-{uuid.uuid4().hex}"
        remote_script = f"/tmp/sandrun-{uuid.uuid4().hex}.sh"

        sandbox.fs.upload_file(script.encode("utf-8"), remote_script)
        command = f"bash -lc {shlex.quote(f'chmod 700 {remote_script} && bash {remote_script}')}"

        try:
            sandbox.process.create_session(session_id)
            exec_resp = sandbox.process.execute_session_command(
                session_id,
                SessionExecuteRequest(command=command, run_async=True),
                timeout=timeout,
            )
            cmd_id = exec_resp.cmd_id

            stdout_offset = 0
            stderr_offset = 0
            exit_code: int | None = None
            last_logs = None
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                time.sleep(poll_interval)

                try:
                    logs = sandbox.process.get_session_command_logs(session_id, cmd_id)
                except Exception:
                    logs = None

                if logs is not None:
                    last_logs = logs
                    cur_stdout = getattr(logs, "stdout", None) or ""
                    cur_stderr = getattr(logs, "stderr", None) or ""

                    if on_stdout and len(cur_stdout) > stdout_offset:
                        new_chunk = cur_stdout[stdout_offset:]
                        lines = new_chunk.split("\n")
                        complete_lines = lines[:-1]
                        for line in complete_lines:
                            on_stdout(line)
                        stdout_offset += len(new_chunk) - len(lines[-1])

                    if on_stderr and len(cur_stderr) > stderr_offset:
                        new_chunk = cur_stderr[stderr_offset:]
                        lines = new_chunk.split("\n")
                        complete_lines = lines[:-1]
                        for line in complete_lines:
                            on_stderr(line)
                        stderr_offset += len(new_chunk) - len(lines[-1])

                try:
                    session = sandbox.process.get_session(session_id)
                    for cmd_info in getattr(session, "commands", None) or []:
                        ec = getattr(cmd_info, "exit_code", None)
                        if ec is not None:
                            exit_code = ec
                            break
                except Exception:
                    pass

                if exit_code is not None:
                    break

            # Final log flush.
            try:
                logs = sandbox.process.get_session_command_logs(session_id, cmd_id)
                if logs is not None:
                    last_logs = logs
                    cur_stdout = getattr(logs, "stdout", None) or ""
                    cur_stderr = getattr(logs, "stderr", None) or ""
                    if on_stdout and len(cur_stdout) > stdout_offset:
                        for line in cur_stdout[stdout_offset:].splitlines():
                            on_stdout(line)
                    if on_stderr and len(cur_stderr) > stderr_offset:
                        for line in cur_stderr[stderr_offset:].splitlines():
                            on_stderr(line)
            except Exception:
                pass

            final_ec = exit_code if exit_code is not None else -1
            final_stdout = (getattr(last_logs, "stdout", None) or "") if last_logs else ""
            final_stderr = (getattr(last_logs, "stderr", None) or "") if last_logs else ""
            return ExecResult(exit_code=final_ec, stdout=final_stdout, stderr=final_stderr)

        finally:
            with contextlib.suppress(Exception):
                sandbox.process.delete_session(session_id)
            with contextlib.suppress(Exception):
                sandbox.process.exec(
                    f"bash -lc {shlex.quote(f'rm -f {remote_script}')}",
                    timeout=30,
                )

    def destroy(self, sandbox_id: str) -> None:
        sandbox = self._sandboxes.pop(sandbox_id, None)
        if sandbox is not None:
            self._client.delete(sandbox)
