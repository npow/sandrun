# sandrun

[![CI](https://github.com/npow/sandrun/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sandrun/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sandrun)](https://pypi.org/project/sandrun/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

Run Python functions and scripts in isolated sandboxes — locally or in the cloud.

## Two ways to use it

### 1. CLI — run a script

```bash
sandrun run script.py
sandrun run --backend daytona --package requests script.py
sandrun run --backend e2b --cpu 4 --memory 8192 train.py -- --epochs 10
```

Your current directory is packaged and shipped to the sandbox. Output streams back in real time. Exit code propagates to the shell.

**PEP 723 inline script metadata is supported automatically.** If your script declares dependencies in a `# /// script` block, sandrun reads them without any `--package` flags:

```python
# /// script
# dependencies = [
#   "requests>=2.28",
#   "numpy",
# ]
# ///

import requests, numpy
...
```

```bash
sandrun run script.py   # dependencies installed automatically
```

Extra `--package` flags are merged and deduplicated with inline deps.

### 2. Decorator — call a function remotely

```python
from sandrun import daytona

@daytona(packages=["httpx"])
def fetch(url: str) -> str:
    import httpx
    return httpx.get(url).text

html = fetch.remote("https://example.com")   # runs in a Daytona sandbox
html = fetch("https://example.com")          # runs locally, unchanged
```

`fn.remote()` dispatches to the sandbox. `fn()` is the original function — unit tests work without touching any sandbox.

---

## Backends

| Backend | Install | Requires | Cold start |
|---------|---------|----------|------------|
| `boxlite` | `sandrun[boxlite]` | KVM or HVF (local) | ~1–2s |
| `daytona` | `sandrun[daytona]` | `DAYTONA_API_KEY` | <100ms |
| `e2b` | `sandrun[e2b]` | `E2B_API_KEY` | ~150ms |

```bash
pip install sandrun[boxlite]    # local microVM, no API key
pip install sandrun[daytona]    # export DAYTONA_API_KEY=...
pip install sandrun[e2b]        # export E2B_API_KEY=...
```

`SANDRUN_BACKEND` sets the default backend for both the CLI and decorator.

---

## CLI

```
sandrun run [OPTIONS] SCRIPT [ARGS...]

  --backend, -b NAME      boxlite (default), daytona, e2b
  --package, -p PKG       pip package to install (repeatable)
  --cpu N                 CPUs (default: 1)
  --memory MB             memory in MB (default: 1024)
  --gpu SPEC              GPU spec (backend-specific)
  --env, -e KEY=VALUE     environment variable (repeatable)
  --timeout SEC           timeout in seconds (default: 300)
  --image IMAGE           sandbox image override
```

Arguments after `SCRIPT` are forwarded to the script. Use `--` to separate sandrun flags from script flags:

```bash
sandrun run -b daytona -e API_KEY=secret script.py -- --verbose --output out.csv
```

Also works as a module: `python -m sandrun run script.py`

---

## Decorator

```python
from sandrun import sandbox, daytona, e2b, boxlite

@daytona(
    packages=["requests", "numpy"],   # pip packages to install
    cpu=2,
    memory=4096,
    gpu="T4",
    env={"API_KEY": "..."},
    timeout=600,
    streaming=True,                   # print sandbox stdout in real time
)
def my_func(x: int) -> float:
    ...

result = my_func.remote(42)   # sandbox
result = my_func(42)          # local
```

`@sandbox(backend="daytona")` is the generic form. `@daytona`, `@e2b`, `@boxlite` are shorthands. All three decorator forms work:

```python
@daytona                        # no parens
@daytona()                      # empty parens
@daytona(packages=["numpy"])    # with options
```

Exceptions raised inside the sandbox propagate locally with their original type.

---

## How it works

Both the CLI and decorator package your working directory with `TarballStager`, upload arguments via `backend.upload()`, execute a generated runner script, and retrieve output via `backend.download()`. No extra dependencies — stdlib only.

The backend interface is a thin abstraction over each provider's SDK: `create`, `exec`, `upload`, `download`, `destroy`. You can use it directly for scripting or custom pipelines:

```python
from sandrun.backends import get_backend

backend = get_backend("daytona")
sandbox_id = backend.create()
result = backend.exec_script_streaming(
    sandbox_id,
    "python -c \"print('hello')\"",
    on_stdout=print,
)
backend.destroy(sandbox_id)
```

---

## Development

```bash
git clone https://github.com/npow/sandrun
cd sandrun
pip install -e ".[dev]"
pytest
ruff check .
```

## License

[Apache 2.0](LICENSE)
