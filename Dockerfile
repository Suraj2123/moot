# StudyLink container.
#
# Two stages: the first builds the React app with Node, the second is the
# runtime -- Python plus the built static bundle. The runtime image has no
# Node in it. Splitting the two keeps ~200MB of build tooling out of the
# image that actually runs.

# ---- 1. build the frontend --------------------------------------------------

FROM node:20-alpine AS web

WORKDIR /web

# Copy the manifest first so the install layer is cached until package.json
# actually changes. A single-line COPY of the whole tree busts the cache on
# every source edit and reinstalls 300MB of node_modules for no reason.
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY web/ ./

# vite.config.ts writes to ../studylink/static; override to a fixed path in
# this stage so the runtime stage can copy from a location it does not have to
# guess.
RUN npm run build -- --outDir /out/static --emptyOutDir

# ---- 2. the runtime ---------------------------------------------------------

FROM python:3.11-slim AS runtime

# libpq is needed by psycopg2 at runtime; build-essential is only for the
# install and is removed in the same layer so it does not survive.
RUN apt-get update && \
    apt-get install --no-install-recommends -y libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Run as a non-root user. A compromised web process should not own the
# filesystem it lives on.
RUN useradd --create-home --shell /bin/bash --uid 1000 app
WORKDIR /app

# Install Python deps first, again for caching.
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# App source, then the built frontend on top of it. The COPY order matters:
# the static bundle has to land at studylink/static, which is where the API
# looks.
COPY --chown=app:app . ./
COPY --from=web --chown=app:app /out/static ./studylink/static

USER app

# Alembic runs on start via the entrypoint, not baked into the image, so one
# image serves every environment.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    STUDYLINK_ENV=production

EXPOSE 8000

# The command is set by the platform (web vs worker) via docker-compose, fly,
# or railway config -- the image itself just knows how to run either.
#
# Web:    uvicorn studylink.api:api --host 0.0.0.0 --port $PORT
# Worker: python scripts/run_worker.py
#
# Migrations are run once at release time, not on every container boot, so
# two web replicas do not race to migrate.
CMD ["sh", "-c", "uvicorn studylink.api:api --host 0.0.0.0 --port ${PORT:-8000}"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1
