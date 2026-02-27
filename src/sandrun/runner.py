"""SandboxRunner — orchestrates sandbox lifecycle without Metaflow.

Layer: Core Execution
May only import from: sandrun.backend, sandrun.stager, sandrun.installer,
                      sandrun._micromamba, stdlib

SandboxRunner is the standalone entry point for running code in a sandbox.
It wires together a backend, a code stager, and a dep installer into a
single cohesive execution unit.

Metaflow integration wraps this via SandboxExecutor in metaflow-sandbox.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import time
from typing import TYPE_CHECKING
from typing import Callable

from sandrun._micromamba import is_compatible_linux_micromamba
from sandrun._micromamba import micromamba_path_export
from sandrun._micromamba import micromamba_stage_upload_spec
from sandrun._micromamba import resolve_micromamba
from sandrun.backend import ExecResult
from sandrun.backend import SandboxConfig

if TYPE_CHECKING:
    from sandrun.backend import SandboxBackend
    from sandrun.installer import DepInstaller
    from sandrun.stager import PackageStager


def _is_hard_minus_one(result: ExecResult) -> bool:
    """Return True if the result is a transient infra error (exit_code -1 with no output)."""
    if result.exit_code != -1:
        return False
    if (result.stdout or "").strip():
        return False
    stderr = (result.stderr or "").strip()
    if not stderr:
        return True
    remaining = [
        line
        for line in stderr.splitlines()
        if line.strip() and not line.lstrip().startswith("[daytona-debug]")
    ]
    return len(remaining) == 0


class SandboxRunner:
    """Run a script in a new sandbox, handling staging and cleanup.

    Parameters
    ----------
    backend:
        The :class:`~sandrun.backend.SandboxBackend` to use for sandbox
        creation and script execution.
    stager:
        Optional :class:`~sandrun.stager.PackageStager` that delivers a
        code package into the sandbox before the script runs.  If ``None``,
        no code package is staged (the image must already contain the code).
    installer:
        Optional :class:`~sandrun.installer.DepInstaller` that stages and
        installs dependencies inside the sandbox.  If ``None``, no deps are
        installed (``NoopDepInstaller`` behaviour).
    stage_micromamba:
        Whether to auto-stage a Linux micromamba binary.  Defaults to ``True``
        when the installer is a :class:`~sandrun.installer.CondaOfflineInstaller`.
        Pass ``False`` to skip, e.g. when the image already has micromamba.
    max_infra_retries:
        How many times to retry after a hard ``-1`` exit code (transient
        infra failures before user code runs).  Defaults to ``1`` (one retry).
    """

    def __init__(
        self,
        backend: "SandboxBackend",
        stager: "PackageStager | None" = None,
        installer: "DepInstaller | None" = None,
        stage_micromamba: bool | None = None,
        max_infra_retries: int | None = None,
    ) -> None:
        self._backend = backend
        self._stager = stager
        self._installer = installer

        # Auto-enable micromamba staging when using CondaOfflineInstaller.
        if stage_micromamba is None:
            from sandrun.installer import CondaOfflineInstaller

            stage_micromamba = isinstance(installer, CondaOfflineInstaller)
        self._stage_micromamba = stage_micromamba

        self._max_infra_retries = (
            max_infra_retries
            if max_infra_retries is not None
            else int(os.environ.get("SANDRUN_MAX_INFRA_RETRIES", "1"))
        )

    def run(
        self,
        script: str,
        config: SandboxConfig,
        extra_uploads: list[dict[str, str]] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Create a sandbox, stage assets, run *script*, and destroy the sandbox.

        Parameters
        ----------
        script:
            The full bash script to execute inside the sandbox.  If a
            :class:`~sandrun.stager.PackageStager` and/or
            :class:`~sandrun.installer.DepInstaller` are configured, their
            setup commands are prepended automatically.
        config:
            Sandbox creation parameters (image, resources, env vars, timeout).
        extra_uploads:
            Additional ``{"local": ..., "remote": ..., "mode": ...}`` dicts
            to upload before running the script (e.g. staged credential files).
        on_stdout / on_stderr:
            Per-line callbacks for streaming output.  When provided, output
            is emitted as it arrives.  When omitted, output is buffered and
            returned in the :class:`~sandrun.backend.ExecResult`.

        Returns
        -------
        ExecResult
            Exit code, combined stdout, and combined stderr from the sandbox.

        Raises
        ------
        SandboxRunnerError
            On unrecoverable infrastructure errors (not on non-zero exit codes
            from user code — callers must inspect ``result.exit_code``).
        """
        micromamba_path, micromamba_available = (
            resolve_micromamba() if self._stage_micromamba else (None, False)
        )
        should_stage_micromamba = self._stage_micromamba and micromamba_available

        full_script = self._build_script(script, should_stage_micromamba)

        attempts = max(1, self._max_infra_retries + 1)
        last_result: ExecResult | None = None

        for attempt in range(attempts):
            sandbox_id = self._backend.create(config)
            try:
                # Stage micromamba binary before user uploads.
                chmod_cmds: list[str] = []
                if should_stage_micromamba and micromamba_path:
                    spec = micromamba_stage_upload_spec(micromamba_path)
                    try:
                        self._backend.upload(sandbox_id, spec["local"], spec["remote"])
                        chmod_cmds.append(
                            f"chmod {shlex.quote(spec['mode'])} {shlex.quote(spec['remote'])}"
                        )
                    except Exception:
                        if self._stage_micromamba:
                            pass  # micromamba staging is best-effort unless required

                # Extra host uploads.
                for upload in extra_uploads or []:
                    self._backend.upload(
                        sandbox_id, upload["local"], upload["remote"]
                    )
                    if "mode" in upload:
                        chmod_cmds.append(
                            f"chmod {shlex.quote(upload['mode'])} "
                            f"{shlex.quote(upload['remote'])}"
                        )

                # Stage code package.
                if self._stager is not None:
                    self._stager.deliver(self._backend, sandbox_id)

                # Stage dependency packages.
                if self._installer is not None:
                    self._installer.stage(self._backend, sandbox_id)

                # Build sandbox-ID-aware run command.
                prefix_parts: list[str] = [
                    f"export SANDRUN_SANDBOX_ID={shlex.quote(sandbox_id)}"
                ]
                if should_stage_micromamba:
                    prefix_parts.append(micromamba_path_export())
                if chmod_cmds:
                    prefix_parts.extend(chmod_cmds)

                run_cmd = " && ".join(prefix_parts) + " && " + full_script

                result = self._backend.exec_script_streaming(
                    sandbox_id,
                    run_cmd,
                    timeout=config.timeout,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                )
                last_result = result

                if not _is_hard_minus_one(result) or attempt == attempts - 1:
                    break

            finally:
                if last_result is not None:
                    # Only destroy after a successful exec; keep alive on retry.
                    if not _is_hard_minus_one(last_result) or attempt == attempts - 1:
                        with contextlib.suppress(Exception):
                            self._backend.destroy(sandbox_id)
                else:
                    with contextlib.suppress(Exception):
                        self._backend.destroy(sandbox_id)

            if attempt < attempts - 1:
                time.sleep(2**attempt)

        if last_result is None:
            raise SandboxRunnerError("Sandbox execution did not produce a result.")
        return last_result

    def _build_script(self, user_script: str, with_micromamba_path: bool) -> str:
        """Prepend stager + installer setup commands to *user_script*."""
        parts: list[str] = []

        if with_micromamba_path:
            parts.append(micromamba_path_export())

        if self._stager is not None:
            parts.extend(self._stager.setup_commands())

        if self._installer is not None:
            parts.extend(self._installer.setup_commands())

        parts.append(user_script)
        return "\n".join(parts)


class SandboxRunnerError(RuntimeError):
    """Raised by :class:`SandboxRunner` on unrecoverable infrastructure errors."""
