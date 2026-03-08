"""High-level function decorators for sandbox execution.

The function's *code* is delivered via TarballStager — the entire CWD is
packaged and extracted in the sandbox, then the function is imported by
module path.  Arguments are uploaded as a pickle file; the return value is
downloaded as a pickle file.  No extra dependencies beyond stdlib.

Usage::

    from sandrun import daytona, e2b, boxlite

    @daytona(packages=["httpx"])
    def fetch(url: str) -> str:
        import httpx
        return httpx.get(url).text

    html = fetch.remote("https://example.com")
    # fetch(url)  -- still calls locally

Can be used with or without arguments::

    @daytona
    def my_func(): ...

    @daytona(packages=["numpy"], cpu=2)
    def my_func(): ...
"""

from __future__ import annotations

import contextlib
import inspect
import os
import pickle
import shlex
import tarfile
import tempfile
from typing import Any
from typing import Callable

from sandrun.backend import Resources
from sandrun.backend import SandboxConfig
from sandrun.backends import get_backend
from sandrun.runner import _is_hard_minus_one
from sandrun.stager import TarballStager

_REMOTE_ARGS = "/tmp/_sandrun_args.pkl"
_REMOTE_RESULT = "/tmp/_sandrun_result.pkl"
_REMOTE_RUNNER = "/tmp/_sandrun_runner.py"

_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
}
_EXCLUDE_EXTENSIONS = {".pyc", ".pyo", ".so", ".dylib", ".dll"}


# ---------------------------------------------------------------------------
# Runner script
# ---------------------------------------------------------------------------


def _build_runner_script(module_spec: str, qualname: str, is_file: bool) -> str:
    """Python script that runs inside the sandbox: loads fn, calls it, writes result."""
    if is_file:
        import_block = (
            "import importlib.util as _ilu\n"
            f"_spec = _ilu.spec_from_file_location('_sandrun_entry', {module_spec!r})\n"
            "_mod = _ilu.module_from_spec(_spec)\n"
            "_spec.loader.exec_module(_mod)\n"
        )
    else:
        import_block = f"import importlib as _il\n_mod = _il.import_module({module_spec!r})\n"

    return f"""\
import sys, os, pickle

# CWD is set to the extracted package dir by TarballStager.
sys.path.insert(0, os.getcwd())

with open({_REMOTE_ARGS!r}, 'rb') as _f:
    _args, _kwargs = pickle.loads(_f.read())

{import_block}
_fn = _mod
for _part in {qualname!r}.split('.'):
    _fn = getattr(_fn, _part)

try:
    _result = _fn(*_args, **_kwargs)
    _outcome = ('ok', _result)
except Exception as _e:
    _outcome = ('err', _e)

with open({_REMOTE_RESULT!r}, 'wb') as _f:
    _f.write(pickle.dumps(_outcome))
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fn_import_spec(fn: Callable) -> tuple[str, str, bool]:
    """Return ``(module_spec, qualname, is_file)`` for *fn*."""
    module = fn.__module__
    qualname = fn.__qualname__

    if module != "__main__":
        return module, qualname, False

    try:
        abs_path = inspect.getfile(fn)
    except (TypeError, OSError) as exc:
        raise SandboxFunctionError(
            "Cannot determine source file for a function defined in __main__. "
            "Define the function in an importable module."
        ) from exc

    try:
        rel_path = os.path.relpath(abs_path)
    except ValueError:
        rel_path = abs_path

    return rel_path, qualname, True


def _exclude_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = os.path.basename(tarinfo.name)
    if tarinfo.isdir() and name in _EXCLUDE_DIRS:
        return None
    if tarinfo.isfile():
        _, ext = os.path.splitext(name)
        if ext in _EXCLUDE_EXTENSIONS:
            return None
        if tarinfo.size > 50 * 1024 * 1024:
            return None
    return tarinfo


def _build_cwd_tarball() -> str:
    fd, path = tempfile.mkstemp(suffix=".tar", prefix="sandrun-code-")
    try:
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w") as tar:
            tar.add(".", filter=_exclude_filter)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path


# ---------------------------------------------------------------------------
# Core wrapper
# ---------------------------------------------------------------------------


def _make_wrapper(
    fn: Callable,
    backend: str,
    packages: list[str] | None,
    image: str | None,
    cpu: int,
    memory: int,
    gpu: str | None,
    env: dict[str, str] | None,
    timeout: int,
    streaming: bool,
) -> Callable:
    module_spec, qualname, is_file = _fn_import_spec(fn)

    def _remote(*args: Any, **kwargs: Any) -> Any:
        tarball_path = _build_cwd_tarball()
        args_fd, args_path = tempfile.mkstemp(suffix=".pkl", prefix="sandrun-args-")
        runner_fd, runner_path = tempfile.mkstemp(suffix=".py", prefix="sandrun-runner-")
        result_fd, result_path = tempfile.mkstemp(suffix=".pkl", prefix="sandrun-result-")
        os.close(result_fd)

        try:
            with os.fdopen(args_fd, "wb") as f:
                f.write(pickle.dumps((args, kwargs)))
            with os.fdopen(runner_fd, "w") as f:
                f.write(_build_runner_script(module_spec, qualname, is_file))

            stager = TarballStager(tarball_path)
            _backend = get_backend(backend) if isinstance(backend, str) else backend
            config = SandboxConfig(
                image=image,
                env=env or {},
                resources=Resources(cpu=cpu, memory_mb=memory, gpu=gpu),
                timeout=timeout,
            )

            pkgs = list(packages or [])
            install_prefix = ("pip install -q " + " ".join(pkgs) + " && ") if pkgs else ""
            # stager.setup_commands() sets CWD to the extracted workdir
            run_cmd = f"{install_prefix}python {shlex.quote(_REMOTE_RUNNER)}"

            max_retries = int(os.environ.get("SANDRUN_MAX_INFRA_RETRIES", "1"))
            attempts = max(1, max_retries + 1)

            for attempt in range(attempts):
                sandbox_id = _backend.create(config)
                try:
                    stager.deliver(_backend, sandbox_id)
                    _backend.upload(sandbox_id, args_path, _REMOTE_ARGS)
                    _backend.upload(sandbox_id, runner_path, _REMOTE_RUNNER)

                    setup = "\n".join(stager.setup_commands())
                    sid = shlex.quote(sandbox_id)
                    script = f"export SANDRUN_SANDBOX_ID={sid}\n{setup}\n{run_cmd}"

                    on_stdout = print if streaming else None

                    result = _backend.exec_script_streaming(
                        sandbox_id,
                        script,
                        timeout=timeout,
                        on_stdout=on_stdout,
                    )

                    if _is_hard_minus_one(result) and attempt < attempts - 1:
                        continue

                    if result.exit_code != 0:
                        raise SandboxFunctionError(
                            f"Remote function failed (exit {result.exit_code}):\n"
                            f"{result.stderr or result.stdout}"
                        )

                    _backend.download(sandbox_id, _REMOTE_RESULT, result_path)
                    break

                finally:
                    with contextlib.suppress(Exception):
                        _backend.destroy(sandbox_id)

            with open(result_path, "rb") as f:
                tag, value = pickle.loads(f.read())

            if tag == "ok":
                return value
            raise value

        finally:
            for path in (tarball_path, args_path, runner_path, result_path):
                with contextlib.suppress(OSError):
                    os.unlink(path)

    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    wrapper.remote = _remote  # type: ignore[attr-defined]
    return wrapper


# ---------------------------------------------------------------------------
# Public decorators
# ---------------------------------------------------------------------------


class SandboxFunctionError(RuntimeError):
    """Raised when a sandboxed function fails for infrastructure reasons."""


def sandbox(
    fn: Callable | None = None,
    *,
    backend: str = "boxlite",
    packages: list[str] | None = None,
    image: str | None = None,
    cpu: int = 1,
    memory: int = 1024,
    gpu: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    streaming: bool = False,
) -> Any:
    """Decorator that adds a ``.remote()`` method to run a function in a sandbox.

    The original function is unchanged — ``fn()`` still calls locally.
    Use ``fn.remote()`` to execute in the sandbox.
    """

    def decorator(f: Callable) -> Callable:
        return _make_wrapper(f, backend, packages, image, cpu, memory, gpu, env, timeout, streaming)

    if fn is not None:
        return decorator(fn)
    return decorator


def _make_backend_decorator(backend_name: str) -> Any:
    def decorator(
        fn: Callable | None = None,
        *,
        packages: list[str] | None = None,
        image: str | None = None,
        cpu: int = 1,
        memory: int = 1024,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
        streaming: bool = False,
    ) -> Any:
        def _dec(f: Callable) -> Callable:
            return _make_wrapper(
                f, backend_name, packages, image, cpu, memory, gpu, env, timeout, streaming
            )

        if fn is not None:
            return _dec(fn)
        return _dec

    decorator.__name__ = backend_name
    decorator.__qualname__ = backend_name
    decorator.__doc__ = (
        f"Decorator that adds ``.remote()`` to run a function in a {backend_name} sandbox.\n\n"
        f"Shorthand for ``@sandbox(backend={backend_name!r}, ...)``.\n"
        "The original function is unchanged — use ``.remote()`` for sandbox execution."
    )
    return decorator


daytona = _make_backend_decorator("daytona")
e2b = _make_backend_decorator("e2b")
boxlite = _make_backend_decorator("boxlite")
