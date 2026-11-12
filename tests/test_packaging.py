"""Version, wheel metadata and image matrix stay consistent."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_versions_match_everywhere():
    core = _project(ROOT / "core" / "pyproject.toml")
    adapters = _project(ROOT / "adapters" / "pyproject.toml")
    assert core["version"] == adapters["version"]
    version = core["version"]
    assert version == "0.0.17"

    import agy_proxy_core
    import agy_proxy_adapters

    assert agy_proxy_core.__version__ == version
    assert agy_proxy_adapters.__version__ == version


def test_adapters_pin_core_exactly():
    adapters = _project(ROOT / "adapters" / "pyproject.toml")
    core_version = _project(ROOT / "core" / "pyproject.toml")["version"]
    assert f"agy-proxy-core=={core_version}" in adapters["dependencies"]


def test_dockerfile_has_two_targets():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM runtime-base AS core" in text
    assert "FROM runtime-base AS full" in text
    assert "agy-proxy-core==0.0.17" in text
    assert "agy-proxy-adapters==0.0.17" in text
    assert 'CMD ["python", "-m", "agy_proxy_core"]' in text
    assert 'CMD ["python", "-m", "agy_proxy_adapters"]' in text


def test_frontend_builds_into_core_package():
    text = (ROOT / "vite.config.ts").read_text(encoding="utf-8")
    assert 'outDir: "core/agy_proxy_core/static"' in text
    assert (ROOT / "core" / "agy_proxy_core" / "static" / "index.html").is_file()
