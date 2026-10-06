#!/usr/bin/env bash
# Repo Analysis Tool (RAT) — install dependencies and start the server.
# Serves the dashboard on http://127.0.0.1:5000 (override with PORT=...).
set -euo pipefail
cd "$(dirname "$0")"

echo "Installing dependencies..."
pip install -q -r requirements.txt

export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
echo "Starting RAT on http://127.0.0.1:${PORT:-5000}"
exec python3 -m server.app "$@"
