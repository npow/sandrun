# sandrun

[![CI](https://github.com/npow/sandrun/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sandrun/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sandrun)](https://pypi.org/project/sandrun/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

Run code in isolated sandboxes — locally or in the cloud — with a single consistent API.

```python
from sandrun.backends import get_backend

backend = get_backend("boxlite")   # or "daytona", "e2b"
sandbox_id = backend.create()
result = backend.exec(sandbox_id, ["python", "-c", "print('hello')"])
print(result.stdout)
backend.destroy(sandbox_id)
```

Switch backends by changing one string. Same API everywhere.

## Quick Start

### Local (no API key)

```bash
pip install sandrun[boxlite]
```

```python
from sandrun.backends import get_backend

backend = get_backend("boxlite")
sandbox_id = backend.create()

result = backend.exec_script_streaming(
    sandbox_id,
    "for i in 1 2 3; do echo step $i; done",
    on_stdout=print,
)
backend.destroy(sandbox_id)
```

Requires KVM (Linux) or Apple Hypervisor Framework (macOS).

### Cloud — Daytona (<100ms cold start)

```bash
pip install sandrun[daytona]
export DAYTONA_API_KEY=...
```

```python
backend = get_backend("daytona")
```

### Cloud — E2B (Firecracker microVM)

```bash
pip install sandrun[e2b]
export E2B_API_KEY=...
```

```python
backend = get_backend("e2b")
```

## Backends

| Backend | Install | Requires | Cold start |
|---------|---------|----------|------------|
| `boxlite` | `sandrun[boxlite]` | KVM or HVF (local) | ~1–2s |
| `daytona` | `sandrun[daytona]` | `DAYTONA_API_KEY` | <100ms |
| `e2b` | `sandrun[e2b]` | `E2B_API_KEY` | ~150ms |

## Usage

### Upload a file, then execute it

```python
backend = get_backend("boxlite")
sandbox_id = backend.create()

backend.upload(sandbox_id, "script.py", "/root/script.py")
result = backend.exec(sandbox_id, ["python", "/root/script.py"])
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

## How It Works

sandrun is a thin orchestration layer with a provider-agnostic `SandboxBackend` interface. Each backend wraps a sandbox SDK behind a uniform API: `create`, `exec`, `upload`, `download`, `destroy`. The `TarballStager` and `CondaOfflineInstaller` handle code and dependency delivery via `backend.upload()`.

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
