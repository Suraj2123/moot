#!/usr/bin/env bash
# Start StudyLink: the API (which serves the web app) and the background worker.
#
#   ./run.sh              start everything
#   ./run.sh --seed       wipe and reseed the demo account first
#
# Then open http://127.0.0.1:8000
set -euo pipefail

cd "$(dirname "$0")"

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PY=${PYTHON:-python3}

# A key is required before anyone can connect Canvas, and there is deliberately
# no plaintext fallback. Generate a local one rather than making the first run
# fail on configuration.
if [ -z "${STUDYLINK_SECRET_KEY:-}" ]; then
  if [ -f .secret_key ]; then
    STUDYLINK_SECRET_KEY="$(cat .secret_key)"
  else
    STUDYLINK_SECRET_KEY="$($PY -c 'from studylink.vault import generate_key; print(generate_key())')"
    echo "$STUDYLINK_SECRET_KEY" > .secret_key
    echo "Generated a local encryption key in .secret_key (gitignored)."
  fi
  export STUDYLINK_SECRET_KEY
fi

if [ ! -f studylink/static/index.html ]; then
  echo "The web app has not been built. Building it now…"
  if command -v npm >/dev/null 2>&1; then
    (cd web && npm install --silent && npm run build)
  else
    echo "npm not found. Install Node, or use the Streamlit UI: streamlit run app.py" >&2
    exit 1
  fi
fi

if [ "${1:-}" = "--seed" ]; then
  $PY scripts/seed_demo.py --account demo@school.edu
fi

# The worker processes indexing and Canvas sync. Without it, notes save but
# never become searchable -- which looks like a bug rather than a missing
# process, so it starts here by default.
$PY scripts/run_worker.py &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT INT TERM

echo
echo "  StudyLink -> http://127.0.0.1:8000"
echo
exec $PY -m uvicorn studylink.api:api --host 127.0.0.1 --port 8000
