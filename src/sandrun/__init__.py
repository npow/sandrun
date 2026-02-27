"""sandrun — run code in fast, isolated cloud sandboxes.

Zero Metaflow dependency.  Pluggable backends (Daytona, E2B, ...).
"""

from sandrun._types import PackageSpec
from sandrun.backend import ExecResult
from sandrun.backend import Resources
from sandrun.backend import SandboxBackend
from sandrun.backend import SandboxConfig
from sandrun.backends import get_backend
from sandrun.decorator import SandboxFunctionError
from sandrun.decorator import boxlite
from sandrun.decorator import daytona
from sandrun.decorator import e2b
from sandrun.decorator import sandbox
from sandrun.installer import CondaOfflineInstaller
from sandrun.installer import DepInstaller
from sandrun.installer import NoopDepInstaller
from sandrun.installer import UvDepInstaller
from sandrun.runner import SandboxRunner
from sandrun.runner import SandboxRunnerError
from sandrun.stager import PackageStager
from sandrun.stager import TarballStager

__all__ = [
    "CondaOfflineInstaller",
    "DepInstaller",
    "ExecResult",
    "NoopDepInstaller",
    "PackageSpec",
    "PackageStager",
    "Resources",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxFunctionError",
    "SandboxRunner",
    "SandboxRunnerError",
    "TarballStager",
    "UvDepInstaller",
    "boxlite",
    "daytona",
    "e2b",
    "get_backend",
    "sandbox",
]
