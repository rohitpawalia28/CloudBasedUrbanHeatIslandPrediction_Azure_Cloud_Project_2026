#!/usr/bin/env bash
# One-click start for macOS / Linux: creates a virtual env, installs packages, launches the cloud console.
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt
python run.py "$@"
