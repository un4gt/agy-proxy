"""Core must never import the adapters package."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core" / "agy_proxy_core"
ADAPTERS = "agy_proxy_adapters"


def _references_adapter(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == ADAPTERS or alias.name.startswith(ADAPTERS + ".") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return bool(node.module and (node.module == ADAPTERS or node.module.startswith(ADAPTERS + ".")))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ADAPTERS in node.value
    return False


def test_core_has_no_adapter_references():
    offenders = []
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_references_adapter(node) for node in ast.walk(tree)):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_core_imports_with_adapters_blocked():
    script = """
import importlib
import importlib.abc
import pkgutil
import sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agy_proxy_adapters" or fullname.startswith("agy_proxy_adapters."):
            raise ImportError("adapters blocked for boundary test")

sys.meta_path.insert(0, Block())
import agy_proxy_core
for module in pkgutil.walk_packages(agy_proxy_core.__path__, "agy_proxy_core."):
    importlib.import_module(module.name)
print("ok")
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "core"), str(ROOT / "adapters"))))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_app_package_is_gone():
    assert not (ROOT / "app").exists()
