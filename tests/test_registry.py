"""Unit tests for sandrun.backends registry."""

from __future__ import annotations

import pytest

from sandrun.backends import _BACKENDS
from sandrun.backends import get_backend


class TestGetBackend:
    def test_unknown_backend_raises_with_available_list(self) -> None:
        with pytest.raises(ValueError, match="Unknown sandbox backend 'nope'"):
            get_backend("nope")

    def test_unknown_backend_error_lists_available(self) -> None:
        with pytest.raises(ValueError, match="Available backends:"):
            get_backend("nope")

    def test_unknown_backend_error_links_docs(self) -> None:
        with pytest.raises(ValueError, match=r"docs/adding-a-backend\.md"):
            get_backend("nope")

    def test_all_registered_backends_have_files(self) -> None:
        from pathlib import Path

        backends_dir = Path(__file__).resolve().parents[1] / "src" / "sandrun" / "backends"
        for name in _BACKENDS:
            assert (backends_dir / f"{name}.py").exists(), (
                f"Backend '{name}' registered but {name}.py not found"
            )

    def test_registry_is_not_empty(self) -> None:
        assert len(_BACKENDS) > 0
