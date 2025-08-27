#!/bin/bash
set -e

# Load .env if present
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Create and use venv to avoid PEP 668
if [ ! -d .venv ]; then
  if python3 -m venv .venv 2>/dev/null; then
    :
  else
    echo "venv not available, will use system pip with --break-system-packages" >&2
    USE_SYSTEM_PIP=1
  fi
fi

if [ -z "$USE_SYSTEM_PIP" ]; then
  if [ -f .venv/bin/activate ]; then
    . .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt --no-cache-dir
  else
    echo "venv activation script missing, fallback to system pip" >&2
    USE_SYSTEM_PIP=1
  fi
fi

if [ -n "$USE_SYSTEM_PIP" ]; then
  pip install -r requirements.txt --no-cache-dir --break-system-packages || true
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$GROQ_API_KEY" ]; then
  echo "TELEGRAM_BOT_TOKEN or GROQ_API_KEY is not set. Running syntax check only." >&2
  python3 -m py_compile legal_bot.py
  exit 0
fi

python3 legal_bot.py