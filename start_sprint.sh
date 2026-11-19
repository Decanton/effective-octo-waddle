#!/usr/bin/env bash
# Launcher for macOS and Linux: ./start_sprint.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
[ -f .venv/.playwright-ok ] || { .venv/bin/python -m playwright install chromium && touch .venv/.playwright-ok; }
exec .venv/bin/python run_sprint.py "$@"
