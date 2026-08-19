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

# --- the worker, in here too unless something else runs it ------------------
#
# The worker belongs in its own process: it is CPU-bound while the web process
# is IO-bound, they want different scaling, and a worker that wedges should not
# take the site down with it. Every platform config in this repo sets them up
# that way and turns this off with RUN_WORKER=0.
#
# The image defaults it on anyway, because the alternative default fails badly
# for exactly the person least able to diagnose it. A free tier often cannot
# add a second service -- Render bills Background Workers from the first one --
# and without a worker, sync and indexing enqueue, return 202, and never run.
# Notes save and are never searchable. Nothing errors. It reads as a broken app
# rather than a missing process, and the fix is a piece of architecture you
# have to already know about.
#
# Two costs, worth stating rather than discovering: background work competes
# with request handling for the same CPU, so a large Canvas sync slows the site
# while it runs; and a web service that sleeps when idle takes its worker with
# it, so jobs advance while someone is using the app rather than on a schedule.
# Both are fine for a demo. Neither is fine for real load -- run the separate
# service by then, and set RUN_WORKER=0 here.
if [ "${RUN_WORKER:-1}" = "1" ]; then
  echo "RUN_WORKER: draining jobs in this container" >&2
  (
    # Restart on exit. Unsupervised here -- nothing else is watching this
    # process, so a crash on one malformed job would otherwise silently end
    # background work for the life of the container.
    #
    # This loop outlives the `exec` below, which replaces this shell. In a
    # container that is correct: when PID 1 exits the kernel tears down the
    # whole PID namespace and takes the loop with it. Run this script bare on
    # a host and the loop survives the command it was started alongside --
    # worth knowing before using it that way.
    while true; do
      python scripts/run_worker.py || \
        echo "worker exited with $?, restarting in 5s" >&2
      sleep 5
    done
  ) &
fi

exec "$@"
