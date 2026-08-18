# Heroku/Render/Railway process types. Fly reads fly.toml instead.
#
# release runs once per deploy, before web or worker starts, so migrations
# apply exactly once regardless of how many web replicas are scaled.
release: alembic upgrade head
web:     uvicorn studylink.api:api --host 0.0.0.0 --port $PORT
worker:  python scripts/run_worker.py
