#!/usr/bin/env bash
# Double-click to open the studio in your browser. No terminal knowledge needed.
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ ! -x .venv/bin/python ]; then
  echo "First run — setting up (about a minute)…"
  python3 -m venv .venv || { echo "Python 3 not found. Install it from python.org."; read -r; exit 1; }
  .venv/bin/pip install --quiet --upgrade pip
fi
# The writing stages need the anthropic SDK; install if missing.
.venv/bin/python -c "import anthropic" 2>/dev/null || {
  echo "Installing dependencies…"; .venv/bin/pip install --quiet anthropic; }
echo "Opening adpipe studio… (close this window to stop the app)"
exec .venv/bin/python pipeline/app.py
