# sandrun

[![CI](https://github.com/npow/sandrun/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sandrun/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sandrun)](https://pypi.org/project/sandrun/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

Run code in fast, isolated cloud sandboxes without touching your local environment.

## The problem

You want to execute untrusted or resource-intensive code in an isolated environment, but spinning up a full VM or container pipeline is slow, complex, and tightly coupled to your infrastructure. Existing sandbox SDKs lock you into a single provider — switching from Daytona to E2B means rewriting your integration from scratch.

## Quick start

```bash
pip install sandrun[daytona]
export DAYTONA_API_KEY=...
```

```python
from sandrun.backends import get_backend

backend = get_backend("daytona")
sandbox_id = backend.create()
result = backend.exec(sandbox_id, ["python", "-c", "print('hello from sandbox')"])
print(result.stdout)   # hello from sandbox
backend.destroy(sandbox_id)
```

## Install

```bash
# Daytona backend
pip install sandrun[daytona]

# E2B backend
pip install sandrun[e2b]

# Both
pip install "sandrun[daytona,e2b]"

# Core only (bring your own backend)
pip install sandrun
```

## Usage

### Run a bash script and stream output

```python
backend = get_backend("daytona")
sandbox_id = backend.create()

result = backend.exec_script_streaming(
    sandbox_id,
    "for i in 1 2 3; do echo step $i; sleep 1; done",
    on_stdout=print,
)
print("exit code:", result.exit_code)
backend.destroy(sandbox_id)
```

### Upload a file, then execute it

```python
backend = get_backend("e2b")
sandbox_id = backend.create()

backend.upload(sandbox_id, "script.py", "/tmp/script.py")
result = backend.exec(sandbox_id, ["python", "/tmp/script.py"])
backend.destroy(sandbox_id)
```

### Deliver a code package and install deps offline

```python
from sandrun.stager import TarballStager
from sandrun.installer import CondaOfflineInstaller

stager = TarballStager("my-project.tar")
installer = CondaOfflineInstaller.from_staged("staging-dir/")

backend = get_backend("daytona")
sandbox_id = backend.create()

stager.deliver(backend, sandbox_id)
installer.stage(backend, sandbox_id)

backend.exec_script(sandbox_id, "\n".join([
    *stager.setup_commands(),
    *installer.setup_commands(),
    "cd /tmp/.sandrun-workdir && python main.py",
]))
backend.destroy(sandbox_id)
```

## How it works

sandrun is a thin orchestration layer with a provider-agnostic `SandboxBackend` interface. Each backend wraps a cloud sandbox SDK (Daytona, E2B) behind a uniform API: `create`, `exec`, `upload`, `download`, `destroy`. The `TarballStager` and `CondaOfflineInstaller` handle code and dependency delivery via `backend.upload()` — no object storage required.

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
