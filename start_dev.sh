#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Creating project virtual environment...'
  python3 -m venv --system-site-packages .venv
fi
echo 'Starting AeroScope locally. Press Ctrl+C to stop.'
exec .venv/bin/python start_dev.py
