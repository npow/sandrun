"""sandrun — run code in fast, isolated cloud sandboxes.

Zero Metaflow dependency.  Pluggable backends (Daytona, E2B, ...).
"""

from sandrun._types import PackageSpec
from sandrun.backend import ExecResult
from sandrun.backend import Resources
from sandrun.backend import SandboxBackend
from sandrun.backend import SandboxConfig
from sandrun.backends import get_backend
from sandrun.installer import CondaOfflineInstaller
from sandrun.installer import DepInstaller
from sandrun.installer import NoopDepInstaller
from sandrun.installer import UvDepInstaller
from sandrun.runner import SandboxRunner
from sandrun.runner import SandboxRunnerError
from sandrun.stager import PackageStager
from sandrun.stager import TarballStager

__all__ = [
    # Core types
    "ExecResult",
    "Resources",
    "SandboxBackend",
    "SandboxConfig",
    "PackageSpec",
    # Backend resolution
    "get_backend",
    # Staging
    "PackageStager",
    "TarballStager",
    # Installation
    "DepInstaller",
    "NoopDepInstaller",
    "CondaOfflineInstaller",
    "UvDepInstaller",
    # Runner
    "SandboxRunner",
    "SandboxRunnerError",
]
