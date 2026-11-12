#!/usr/bin/env python3
"""Fail when the two distributions, package metadata or image matrix drift."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]


def main() -> int:
    core = project(ROOT / "core" / "pyproject.toml")
    adapters = project(ROOT / "adapters" / "pyproject.toml")
    version = core["version"]
    errors = []
    if adapters["version"] != version:
        errors.append(f"version drift: core={version} adapters={adapters['version']}")
    pin = f"agy-proxy-core=={version}"
    if pin not in adapters["dependencies"]:
        errors.append(f"adapters dependency must pin {pin}")

    sys.path[:0] = [str(ROOT / "core"), str(ROOT / "adapters")]
    import agy_proxy_adapters
    import agy_proxy_core

    if agy_proxy_core.__version__ != version:
        errors.append(f"agy_proxy_core.__version__={agy_proxy_core.__version__}")
    if agy_proxy_adapters.__version__ != version:
        errors.append(f"agy_proxy_adapters.__version__={agy_proxy_adapters.__version__}")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for marker in (
        "FROM runtime-base AS core",
        "FROM runtime-base AS full",
        f"agy-proxy-core=={version}",
        f"agy-proxy-adapters=={version}",
        f'org.opencontainers.image.version="{version}"',
    ):
        if marker not in dockerfile:
            errors.append(f"Dockerfile missing: {marker}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"versions ok: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
