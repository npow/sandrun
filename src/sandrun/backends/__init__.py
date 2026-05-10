"""Sandbox backend registry.

Layer: Backend Resolution
May only import from: sandrun.backend (ABC), concrete backend modules

Backends are resolved lazily — the SDK for a provider is only
imported when that backend is actually used. This keeps
``pip install sandrun`` lightweight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sandrun.backend import SandboxBackend

# Registry of backend name -> module path + class name.
# Add new backends here.
_BACKENDS: dict[str, tuple[str, str]] = {
    "daytona": (".daytona", "DaytonaBackend"),
    "e2b": (".e2b", "E2BBackend"),
    "boxlite": (".boxlite", "BoxliteBackend"),
}


def get_backend(name: str) -> SandboxBackend:
    """Resolve a backend by name with lazy import.

    Raises a helpful error if the backend's SDK is not installed,
    following the "linter errors as teaching" pattern — every
    failure message tells you exactly how to fix it.
    """
    if name not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"Unknown sandbox backend '{name}'. "
            f"Available backends: {available}. "
            f"To add a new backend, see docs/adding-a-backend.md"
        )

    module_path, class_name = _BACKENDS[name]

    import importlib

    module = importlib.import_module(module_path, package=__name__)
    cls = getattr(module, class_name)
    instance: SandboxBackend = cls()
    return instance
