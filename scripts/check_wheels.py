#!/usr/bin/env python3
"""Build both wheels and inspect their contents and dependency pin."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(dest: Path) -> list[Path]:
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "-w", str(dest), str(ROOT / "core"), str(ROOT / "adapters"),
        ],
        check=True,
        cwd=ROOT,
    )
    return sorted(dest.glob("*.whl"))


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        wheels = build(Path(temporary))
        core = next(w for w in wheels if w.name.startswith("agy_proxy_core-"))
        adapters = next(w for w in wheels if w.name.startswith("agy_proxy_adapters-"))
        errors = []
        with zipfile.ZipFile(core) as archive:
            names = set(archive.namelist())
            if not any(name.endswith("agy_proxy_core/static/index.html") for name in names):
                errors.append("core wheel is missing static/index.html")
            if not any(name.endswith("agy_proxy_core/bridge/patcher.py") for name in names):
                errors.append("core wheel is missing bridge/patcher.py")
            if any("agy_proxy_adapters/" in name for name in names):
                errors.append("core wheel contains adapters code")
        with zipfile.ZipFile(adapters) as archive:
            names = set(archive.namelist())
            if not any(name.endswith("agy_proxy_adapters/openai_api.py") for name in names):
                errors.append("adapters wheel is missing openai_api.py")
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8", "replace")
            if not re.search(r"^Requires-Dist: agy-proxy-core==0\.0\.17$", metadata, re.M):
                errors.append("adapters wheel does not pin agy-proxy-core==0.0.17")
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("wheels ok:", core.name, adapters.name)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
