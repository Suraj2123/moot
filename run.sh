#!/usr/bin/env bash
# Start StudyLink: the API (which also serves the web app) and the worker.
#
#   ./run.sh              start everything
#   ./run.sh --seed       wipe and reseed the demo account first
#   ./run.sh --no-install skip the dependency check
#
# Then open http://127.0.0.1:8000
#
# This script exists so a fresh clone runs with one command. Every check below
# is here because its absence produced a confusing failure rather than a clear
# one -- a traceback about a missing module is a worse way to learn that
# dependencies are not installed than being told so.
set -euo pipefail

cd "$(dirname "$0")"

SEED=0
INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --seed) SEED=1 ;;
    --no-install) INSTALL=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- python env

if [ -z "${VIRTUAL_ENV:-}" ] && [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found." >&2; exit 1; }

if [ "$INSTALL" = "1" ]; then
  # Checking for the imports the app actually needs beats checking whether a
  # venv exists: an existing venv with nothing installed is the case that
  # produced a traceback instead of an explanation.
  if ! "$PY" - <<'EOF' >/dev/null 2>&1
import sqlalchemy, fastapi, uvicorn, numpy, alembic  # noqa: F401
EOF
  then
    if [ -z "${VIRTUAL_ENV:-}" ] && [ ! -d .venv ]; then
      echo "Creating a virtual environment in .venv…"
      "$PY" -m venv .venv
      # shellcheck disable=SC1091
      source .venv/bin/activate
      PY=python
    fi
    echo "Installing Python dependencies…"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    echo "Dependencies installed."
  fi
fi

# ------------------------------------------------------------------ the key

# Connecting Canvas is refused without an encryption key, and there is
# deliberately no plaintext fallback -- so generate a local one rather than
# making the first run fail on configuration.
if [ -z "${STUDYLINK_SECRET_KEY:-}" ]; then
  if [ -f .secret_key ]; then
    STUDYLINK_SECRET_KEY="$(cat .secret_key)"
  else
    STUDYLINK_SECRET_KEY="$("$PY" -c 'from studylink.vault import generate_key; print(generate_key())')"
    echo "$STUDYLINK_SECRET_KEY" > .secret_key
    chmod 600 .secret_key
    echo "Generated a local encryption key in .secret_key (gitignored)."
  fi
  export STUDYLINK_SECRET_KEY
fi

# ---------------------------------------------------------------- the frontend

if [ ! -f studylink/static/index.html ]; then
  echo "The web app has not been built. Building it now…"
  if command -v npm >/dev/null 2>&1; then
    (cd web && npm install --silent && npm run build)
  else
    echo "npm not found. Install Node 18+, then re-run. The API still works:" >&2
    echo "  $PY -m uvicorn studylink.api:api" >&2
    exit 1
  fi
fi

# --------------------------------------------------------------------- data

if [ "$SEED" = "1" ]; then
  "$PY" scripts/seed_demo.py --account demo@school.edu
fi

# ------------------------------------------------------------------- run it

# The worker does indexing and Canvas sync. Without it notes save but never
# become searchable, which reads as a bug rather than a missing process.
"$PY" scripts/run_worker.py &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT INT TERM

echo
echo "  StudyLink  ->  http://127.0.0.1:8000"
if [ "$SEED" = "1" ]; then
  echo "  sign in    ->  demo@school.edu / correct horse battery"
fi
echo
exec "$PY" -m uvicorn studylink.api:api --host 127.0.0.1 --port "${PORT:-8000}"
