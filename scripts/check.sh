#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}:${PWD}/core:${PWD}/adapters${PYTHONPATH:+:${PYTHONPATH}}"
python scripts/check_versions.py
python -m pytest -q
python scripts/check_wheels.py
