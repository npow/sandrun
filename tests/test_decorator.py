"""Unit tests for sandrun.decorator (@sandbox, @daytona, @e2b, @boxlite)."""

from __future__ import annotations

import pickle
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from sandrun.backend import ExecResult
from sandrun.decorator import SandboxFunctionError
from sandrun.decorator import boxlite
from sandrun.decorator import daytona
from sandrun.decorator import e2b
from sandrun.decorator import sandbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backend(outcome: tuple, exit_code: int = 0) -> MagicMock:
    """Return a mock backend whose download() writes a pickled outcome."""
    backend = MagicMock()
    backend.create.return_value = "sb-test-1"
    backend.exec_script_streaming.return_value = ExecResult(
        exit_code=exit_code, stdout="", stderr=""
    )

    def _fake_download(sandbox_id: str, remote: str, local: str) -> None:
        with open(local, "wb") as f:
            f.write(pickle.dumps(outcome))

    backend.download.side_effect = _fake_download
    return backend


# ---------------------------------------------------------------------------
# Smoke: decorator forms
# ---------------------------------------------------------------------------

class TestDecoratorForms:
    def _backend(self) -> MagicMock:
        return _make_backend(("ok", 42))

    def test_no_parens(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @daytona
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_empty_parens(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @daytona()
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_with_packages(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @daytona(packages=["requests"])
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_sandbox_generic(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @sandbox(backend="daytona")
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_e2b(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @e2b
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_boxlite(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=self._backend()):
            @boxlite
            def fn() -> int:
                return 42
            assert fn.remote() == 42

    def test_local_call_unchanged(self) -> None:
        """fn() calls the original function locally, never touches the backend."""
        with patch("sandrun.decorator.get_backend") as mock_get:
            @daytona
            def fn(x: int) -> int:
                return x * 2
            assert fn(5) == 10
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------

class TestResultSerialization:
    def test_returns_primitive(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=_make_backend(("ok", 99))):
            @daytona
            def fn() -> int:
                return 99
            assert fn.remote() == 99

    def test_returns_dict(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=_make_backend(("ok", {"a": 1}))):
            @daytona
            def fn() -> dict:
                return {"a": 1}
            assert fn.remote() == {"a": 1}

    def test_returns_none(self) -> None:
        with patch("sandrun.decorator.get_backend", return_value=_make_backend(("ok", None))):
            @daytona
            def fn() -> None:
                pass
            assert fn.remote() is None

    def test_remote_exception_reraise(self) -> None:
        exc = ValueError("remote boom")
        with patch("sandrun.decorator.get_backend", return_value=_make_backend(("err", exc))):
            @daytona
            def fn() -> None:
                raise ValueError("remote boom")
            with pytest.raises(ValueError, match="remote boom"):
                fn.remote()


# ---------------------------------------------------------------------------
# Infrastructure errors
# ---------------------------------------------------------------------------

class TestInfraErrors:
    def test_nonzero_exit_raises(self) -> None:
        mock_backend = MagicMock()
        mock_backend.create.return_value = "sb-1"
        mock_backend.exec_script_streaming.return_value = ExecResult(
            exit_code=1, stdout="", stderr="oops"
        )
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona
            def fn() -> None:
                pass
            with pytest.raises(SandboxFunctionError, match="exit 1"):
                fn.remote()


# ---------------------------------------------------------------------------
# Sandbox lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_creates_and_destroys(self) -> None:
        mock_backend = _make_backend(("ok", 1))
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona
            def fn() -> int:
                return 1
            fn.remote()
        mock_backend.create.assert_called_once()
        mock_backend.destroy.assert_called_once_with("sb-test-1")

    def test_uploads_args_and_runner(self) -> None:
        mock_backend = _make_backend(("ok", 1))
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona
            def fn() -> int:
                return 1
            fn.remote()
        remote_paths = [call.args[2] for call in mock_backend.upload.call_args_list]
        assert "/tmp/_sandrun_args.pkl" in remote_paths
        assert "/tmp/_sandrun_runner.py" in remote_paths

    def test_downloads_result(self) -> None:
        mock_backend = _make_backend(("ok", 1))
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona
            def fn() -> int:
                return 1
            fn.remote()
        mock_backend.download.assert_called_once()
        _, remote, _ = mock_backend.download.call_args[0]
        assert remote == "/tmp/_sandrun_result.pkl"

    def test_packages_in_script(self) -> None:
        mock_backend = _make_backend(("ok", 1))
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona(packages=["requests", "numpy"])
            def fn() -> int:
                return 1
            fn.remote()
        script = mock_backend.exec_script_streaming.call_args[0][1]
        assert "requests" in script
        assert "numpy" in script

    def test_destroys_on_exec_exception(self) -> None:
        mock_backend = MagicMock()
        mock_backend.create.return_value = "sb-err"
        mock_backend.exec_script_streaming.side_effect = RuntimeError("gone")
        with patch("sandrun.decorator.get_backend", return_value=mock_backend):
            @daytona
            def fn() -> None:
                pass
            with pytest.raises(RuntimeError, match="gone"):
                fn.remote()
        mock_backend.destroy.assert_called_once_with("sb-err")

    def test_backend_selection(self) -> None:
        mock_backend = _make_backend(("ok", 1))
        with patch("sandrun.decorator.get_backend", return_value=mock_backend) as mock_get:
            @e2b
            def fn() -> int:
                return 1
            fn.remote()
        mock_get.assert_called_once_with("e2b")
