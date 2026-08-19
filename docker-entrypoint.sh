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

# --- the worker, optionally in here too -------------------------------------
#
# The worker belongs in its own process: it is CPU-bound while the web process
# is IO-bound, they want different scaling, and a worker that wedges should not
# take the site down with it. Every platform config in this repo sets them up
# that way.
#
# But on a free tier a second service is often the thing you cannot have --
# Render bills Background Workers from the first one -- and the failure that
# produces is quietly bad: sync and indexing queue up, return 202, and never
# run. Notes save and are never searchable. That reads as a broken app rather
# than a missing process.
#
# So: opt in with RUN_WORKER=1 and the web container drains the queue as well.
# Two things to know before using it. Sync competes with request handling for
# the same CPU, so a big Canvas pull will slow the site down. And a free web
# service that sleeps when idle takes its worker down with it, so jobs run when
# someone is using the app, not on a schedule. Both are fine for a demo and
# neither is fine for real load -- run the separate service by then.
if [ "${RUN_WORKER:-0}" = "1" ]; then
  echo "RUN_WORKER=1: draining jobs in the web container" >&2
  (
    # Restart on exit. Unsupervised here -- nothing else is watching this
    # process, so a crash on one malformed job would otherwise silently end
    # background work for the life of the container.
    while true; do
      python scripts/run_worker.py || \
        echo "worker exited with $?, restarting in 5s" >&2
      sleep 5
    done
  ) &
fi

exec "$@"
