"""Unit tests for sandrun._types."""

from __future__ import annotations

import pytest

from sandrun._types import PackageSpec


class TestPackageSpec:
    def test_conda_spec(self) -> None:
        spec = PackageSpec(
            url="https://example.com/numpy-1.24.0-py311h.conda",
            filename="numpy-1.24.0-py311h.conda",
            pkg_type="conda",
            hashes={"md5": "abc123"},
        )
        assert spec.pkg_type == "conda"
        assert spec.is_real_url is True
        assert spec.hashes == {"md5": "abc123"}

    def test_pip_spec(self) -> None:
        spec = PackageSpec(
            url="https://files.pythonhosted.org/packages/requests-2.31.0-py3-none-any.whl",
            filename="requests-2.31.0-py3-none-any.whl",
            pkg_type="pip",
        )
        assert spec.pkg_type == "pip"
        assert spec.hashes == {}

    def test_invalid_pkg_type_raises(self) -> None:
        with pytest.raises(ValueError, match="must be 'conda' or 'pip'"):
            PackageSpec(url="http://x.com/x.tar", filename="x.tar", pkg_type="rpm")

    def test_synthetic_package_not_real_url(self) -> None:
        spec = PackageSpec(
            url="file:///local/pkg.conda",
            filename="pkg.conda",
            pkg_type="conda",
            is_real_url=False,
        )
        assert spec.is_real_url is False

    def test_frozen(self) -> None:
        spec = PackageSpec(url="http://x.com/p.whl", filename="p.whl", pkg_type="pip")
        with pytest.raises(AttributeError):  # FrozenInstanceError is a subclass
            spec.url = "something else"  # type: ignore[misc]

    def test_environment_marker(self) -> None:
        spec = PackageSpec(
            url="http://x.com/p.whl",
            filename="p.whl",
            pkg_type="pip",
            environment_marker="python_version < '3.9'",
        )
        assert spec.environment_marker == "python_version < '3.9'"
