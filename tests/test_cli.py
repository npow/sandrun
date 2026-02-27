"""Unit tests for sandrun.cli (sandrun run ...)."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from sandrun.backend import ExecResult
from sandrun.cli import _build_parser
from sandrun.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(exit_code: int = 0) -> MagicMock:
    runner = MagicMock()
    runner.run.return_value = ExecResult(exit_code=exit_code, stdout="hello", stderr="")
    return runner


def _run(argv: list[str], runner: MagicMock | None = None) -> int:
    """Call main() and return the exit code (captured via SystemExit)."""
    if runner is None:
        runner = _make_runner()
    with (
        patch("sandrun.cli.get_backend"),
        patch("sandrun.cli._build_cwd_tarball", return_value="/tmp/fake.tar"),
        patch("sandrun.cli.TarballStager"),
        patch("sandrun.cli.SandboxRunner", return_value=runner),
        patch("os.unlink"),
        pytest.raises(SystemExit) as exc,
    ):
        main(argv)
    return exc.value.code


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_run_requires_script(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["run"])

    def test_defaults(self) -> None:
        args = _build_parser().parse_args(["run", "script.py"])
        assert args.backend == "boxlite"
        assert args.packages == []
        assert args.cpu == 1
        assert args.memory == 1024
        assert args.gpu is None
        assert args.env == []
        assert args.timeout == 300
        assert args.image is None

    def test_backend_flag(self) -> None:
        args = _build_parser().parse_args(["run", "--backend", "daytona", "script.py"])
        assert args.backend == "daytona"

    def test_short_backend_flag(self) -> None:
        args = _build_parser().parse_args(["run", "-b", "e2b", "script.py"])
        assert args.backend == "e2b"

    def test_packages(self) -> None:
        args = _build_parser().parse_args(["run", "-p", "requests", "-p", "numpy", "script.py"])
        assert args.packages == ["requests", "numpy"]

    def test_resources(self) -> None:
        args = _build_parser().parse_args(
            ["run", "--cpu", "4", "--memory", "8192", "--gpu", "T4", "script.py"]
        )
        assert args.cpu == 4
        assert args.memory == 8192
        assert args.gpu == "T4"

    def test_env(self) -> None:
        args = _build_parser().parse_args(["run", "-e", "FOO=bar", "-e", "BAZ=qux", "script.py"])
        assert args.env == ["FOO=bar", "BAZ=qux"]

    def test_script_args(self) -> None:
        args = _build_parser().parse_args(["run", "script.py", "--flag", "value"])
        assert args.script == "script.py"
        assert args.script_args == ["--flag", "value"]

    def test_script_args_after_separator(self) -> None:
        # argparse REMAINDER strips the "--" separator itself
        args = _build_parser().parse_args(["run", "script.py", "--", "--flag", "value"])
        assert args.script_args == ["--flag", "value"]


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_success(self) -> None:
        assert _run(["run", "script.py"], _make_runner(0)) == 0

    def test_nonzero_propagated(self) -> None:
        assert _run(["run", "script.py"], _make_runner(1)) == 1

    def test_no_command_exits_1(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Script invocation in sandbox
# ---------------------------------------------------------------------------

class TestScriptInvocation:
    def _get_run_script(self, argv: list[str]) -> str:
        runner = _make_runner()
        _run(argv, runner)
        return runner.run.call_args[0][0]

    def test_script_in_command(self) -> None:
        script = self._get_run_script(["run", "script.py"])
        assert "script.py" in script

    def test_packages_in_command(self) -> None:
        script = self._get_run_script(["run", "-p", "requests", "-p", "numpy", "script.py"])
        assert "requests" in script
        assert "numpy" in script
        assert script.index("pip install") < script.index("python")

    def test_script_args_forwarded(self) -> None:
        script = self._get_run_script(["run", "script.py", "--foo", "bar"])
        assert "--foo" in script
        assert "bar" in script

    def test_separator_stripped(self) -> None:
        script = self._get_run_script(["run", "script.py", "--", "--foo"])
        assert "-- " not in script
        assert "--foo" in script

    def test_env_passed_to_config(self) -> None:
        runner = _make_runner()
        with (
            patch("sandrun.cli.get_backend"),
            patch("sandrun.cli._build_cwd_tarball", return_value="/tmp/fake.tar"),
            patch("sandrun.cli.TarballStager"),
            patch("sandrun.cli.SandboxRunner", return_value=runner),
            patch("os.unlink"),
            pytest.raises(SystemExit),
        ):
            main(["run", "-e", "FOO=bar", "script.py"])
        config = runner.run.call_args[0][1]
        assert config.env == {"FOO": "bar"}

    def test_invalid_env_exits_1(self) -> None:
        assert _run(["run", "-e", "NOEQUALSSIGN", "script.py"]) == 1

    def test_resources_in_config(self) -> None:
        runner = _make_runner()
        with (
            patch("sandrun.cli.get_backend"),
            patch("sandrun.cli._build_cwd_tarball", return_value="/tmp/fake.tar"),
            patch("sandrun.cli.TarballStager"),
            patch("sandrun.cli.SandboxRunner", return_value=runner),
            patch("os.unlink"),
            pytest.raises(SystemExit),
        ):
            main(["run", "--cpu", "4", "--memory", "8192", "--gpu", "T4", "script.py"])
        config = runner.run.call_args[0][1]
        assert config.resources.cpu == 4
        assert config.resources.memory_mb == 8192
        assert config.resources.gpu == "T4"
