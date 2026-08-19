#!/bin/sh
# Bring the schema up to date, then hand off to whatever the container was
# asked to run -- the web process or the worker.
#
# `exec` matters: it replaces this shell with the real process so signals from
# the platform reach it directly. Without it, a stop request goes to /bin/sh,
# uvicorn never hears it, and every deploy waits out the kill timeout.
set -e

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  python scripts/migrate.py
fi

exec "$@"
