"""Unit tests for sandrun.runner.SandboxRunner."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from sandrun.backend import ExecResult
from sandrun.backend import Resources
from sandrun.backend import SandboxConfig
from sandrun.installer import NoopDepInstaller
from sandrun.runner import SandboxRunner
from sandrun.runner import SandboxRunnerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backend(exit_code: int = 0) -> MagicMock:
    backend = MagicMock()
    backend.create.return_value = "sb-test-1"
    backend.exec_script_streaming.return_value = ExecResult(
        exit_code=exit_code, stdout="ok", stderr=""
    )
    return backend


def _make_config() -> SandboxConfig:
    return SandboxConfig(image="python:3.11-slim", timeout=60)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSandboxRunnerLifecycle:
    def test_creates_and_destroys_sandbox(self) -> None:
        backend = _make_backend()
        runner = SandboxRunner(backend, stage_micromamba=False)
        runner.run("echo hello", _make_config())

        backend.create.assert_called_once()
        backend.destroy.assert_called_once_with("sb-test-1")

    def test_destroys_sandbox_on_exception(self) -> None:
        backend = _make_backend()
        backend.exec_script_streaming.side_effect = RuntimeError("boom")
        runner = SandboxRunner(backend, stage_micromamba=False)

        with pytest.raises(RuntimeError, match="boom"):
            runner.run("echo hello", _make_config())

        backend.destroy.assert_called_once_with("sb-test-1")

    def test_returns_exec_result(self) -> None:
        backend = _make_backend(exit_code=0)
        runner = SandboxRunner(backend, stage_micromamba=False)
        result = runner.run("echo hello", _make_config())
        assert isinstance(result, ExecResult)
        assert result.exit_code == 0
        assert result.stdout == "ok"


class TestSandboxRunnerStager:
    def test_stager_deliver_called(self) -> None:
        backend = _make_backend()
        stager = MagicMock()
        stager.setup_commands.return_value = []
        runner = SandboxRunner(backend, stager=stager, stage_micromamba=False)
        runner.run("echo hello", _make_config())

        stager.deliver.assert_called_once_with(backend, "sb-test-1")

    def test_stager_commands_prepended(self) -> None:
        backend = _make_backend()
        stager = MagicMock()
        stager.setup_commands.return_value = ["cd /workdir"]
        runner = SandboxRunner(backend, stager=stager, stage_micromamba=False)
        runner.run("python script.py", _make_config())

        call_args = backend.exec_script_streaming.call_args
        script = call_args[0][1]
        assert "cd /workdir" in script
        assert "python script.py" in script
        assert script.index("cd /workdir") < script.index("python script.py")


class TestSandboxRunnerInstaller:
    def test_installer_stage_called(self) -> None:
        backend = _make_backend()
        installer = MagicMock()
        installer.setup_commands.return_value = []
        runner = SandboxRunner(backend, installer=installer, stage_micromamba=False)
        runner.run("echo hello", _make_config())

        installer.stage.assert_called_once_with(backend, "sb-test-1")

    def test_installer_commands_in_script(self) -> None:
        backend = _make_backend()
        installer = MagicMock()
        installer.setup_commands.return_value = ["pip install --no-index foo"]
        runner = SandboxRunner(backend, installer=installer, stage_micromamba=False)
        runner.run("python script.py", _make_config())

        call_args = backend.exec_script_streaming.call_args
        script = call_args[0][1]
        assert "pip install --no-index foo" in script

    def test_noop_installer_no_stage_calls(self) -> None:
        backend = _make_backend()
        runner = SandboxRunner(
            backend, installer=NoopDepInstaller(), stage_micromamba=False
        )
        runner.run("echo hello", _make_config())
        # backend.exec is never called from stage() (noop)
        backend.exec.assert_not_called()


class TestSandboxRunnerInfraRetry:
    def test_retries_on_hard_minus_one(self) -> None:
        backend = MagicMock()
        backend.create.side_effect = ["sb-1", "sb-2"]
        hard_minus = ExecResult(exit_code=-1, stdout="", stderr="")
        success = ExecResult(exit_code=0, stdout="ok", stderr="")
        backend.exec_script_streaming.side_effect = [hard_minus, success]

        runner = SandboxRunner(backend, stage_micromamba=False, max_infra_retries=1)
        result = runner.run("echo hello", _make_config())

        assert result.exit_code == 0
        assert backend.create.call_count == 2

    def test_no_retry_on_user_error(self) -> None:
        backend = _make_backend(exit_code=1)
        runner = SandboxRunner(backend, stage_micromamba=False, max_infra_retries=1)
        result = runner.run("false", _make_config())

        assert result.exit_code == 1
        assert backend.create.call_count == 1

    def test_sandbox_id_in_script(self) -> None:
        backend = _make_backend()
        runner = SandboxRunner(backend, stage_micromamba=False)
        runner.run("echo hello", _make_config())

        call_args = backend.exec_script_streaming.call_args
        script = call_args[0][1]
        assert "SANDRUN_SANDBOX_ID" in script
        assert "sb-test-1" in script
