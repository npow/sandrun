"""Shared type definitions for sandrun.

No external dependencies — stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field


@dataclass(frozen=True)
class PackageSpec:
    """A pinned, downloadable package specification for dependency staging.

    Instances are produced by reading PEP 723 inline script metadata (standalone
    use) or by converting a ``ResolvedEnvironment`` from nflx-conda (Metaflow use).
    Both paths converge on this type before the three-phase dep-install pipeline.

    Fields
    ------
    url:
        Fully-qualified download URL for the package artifact.
    filename:
        Expected filename on disk.  Used as the local cache key.
    pkg_type:
        ``"conda"`` for conda packages (.conda / .tar.bz2) or ``"pip"`` for
        Python wheel / sdist packages.
    hashes:
        Content hashes keyed by algorithm name, e.g.
        ``{"md5": "abc123", "sha256": "def456"}``.  Used for download
        verification.  May be empty if the upstream resolver did not provide
        hashes (verification is then skipped).
    is_real_url:
        ``False`` for synthetic / locally-built packages whose URL should not
        be fetched from the network.  The installer skips these entirely.
    url_format:
        File extension, e.g. ``".conda"``, ``".tar.bz2"``, ``".whl"``.
        Auto-detected from ``filename`` if left empty.
    environment_marker:
        PEP 508 environment marker, e.g. ``"python_version < '3.9'"``.
        Currently informational only — installers do not evaluate markers.
    """

    url: str
    filename: str
    pkg_type: str  # "conda" or "pip"
    hashes: dict[str, str] = field(default_factory=dict)
    is_real_url: bool = True
    url_format: str = ""
    environment_marker: str | None = None

    def __post_init__(self) -> None:
        if self.pkg_type not in ("conda", "pip"):
            raise ValueError(
                f"PackageSpec.pkg_type must be 'conda' or 'pip', got {self.pkg_type!r}"
            )
