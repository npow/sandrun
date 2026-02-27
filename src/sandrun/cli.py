"""sandrun CLI — run a Python script in an isolated sandbox.

Usage::

    sandrun run script.py
    sandrun run --backend daytona --package requests script.py
    sandrun run --backend e2b --cpu 4 --memory 8192 script.py arg1 arg2
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import sys

from sandrun.backend import Resources
from sandrun.backend import SandboxConfig
from sandrun.backends import get_backend
from sandrun.decorator import _build_cwd_tarball
from sandrun.runner import SandboxRunner
from sandrun.stager import TarballStager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandrun",
        description="Run code in isolated sandboxes — locally or in the cloud.",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="Run a Python script in a sandbox.",
        description=(
            "Package the current directory and run SCRIPT inside a sandbox.\n"
            "Arguments after SCRIPT are forwarded to the script."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("script", help="Python script to run (relative to CWD)")
    run.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        metavar="...",
        help="Arguments forwarded to the script",
    )
    run.add_argument(
        "--backend", "-b",
        default=os.environ.get("SANDRUN_BACKEND", "boxlite"),
        metavar="NAME",
        help="Backend: boxlite (default), daytona, e2b",
    )
    run.add_argument(
        "--package", "-p",
        action="append",
        dest="packages",
        default=[],
        metavar="PKG",
        help="Pip package to install before running (repeatable)",
    )
    run.add_argument("--cpu", type=int, default=1, metavar="N", help="CPUs (default: 1)")
    run.add_argument(
        "--memory", type=int, default=1024, metavar="MB", help="Memory in MB (default: 1024)"
    )
    run.add_argument("--gpu", default=None, metavar="SPEC", help="GPU spec (backend-specific)")
    run.add_argument(
        "--env", "-e",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment variable (repeatable)",
    )
    run.add_argument(
        "--timeout", type=int, default=300, metavar="SEC", help="Timeout in seconds (default: 300)"
    )
    run.add_argument("--image", default=None, metavar="IMAGE", help="Sandbox image override")

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    # Parse KEY=VALUE env pairs.
    env: dict[str, str] = {}
    for kv in args.env:
        if "=" not in kv:
            print(f"sandrun: --env requires KEY=VALUE format, got: {kv!r}", file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        env[k] = v

    # Strip leading "--" separator if present (e.g. sandrun run script.py -- --flag).
    script_args = args.script_args
    if script_args and script_args[0] == "--":
        script_args = script_args[1:]

    # Build the command that runs inside the sandbox.
    install_prefix = ""
    if args.packages:
        install_prefix = "pip install -q " + " ".join(args.packages) + " && "

    remote_cmd = install_prefix + "python " + shlex.quote(args.script)
    if script_args:
        remote_cmd += " " + " ".join(shlex.quote(a) for a in script_args)

    tarball_path = _build_cwd_tarball()
    try:
        stager = TarballStager(tarball_path)
        backend = get_backend(args.backend)
        config = SandboxConfig(
            image=args.image,
            env=env,
            resources=Resources(cpu=args.cpu, memory_mb=args.memory, gpu=args.gpu),
            timeout=args.timeout,
        )

        runner = SandboxRunner(backend=backend, stager=stager, stage_micromamba=False)
        result = runner.run(
            remote_cmd,
            config,
            on_stdout=lambda line: print(line),
            on_stderr=lambda line: print(line, file=sys.stderr),
        )
        return result.exit_code

    finally:
        with contextlib.suppress(OSError):
            os.unlink(tarball_path)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(_cmd_run(args))
