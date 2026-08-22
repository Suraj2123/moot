"""The migrations actually run, on both backends.

This file exists because of a bug it would have caught. Migration 0011 adds a
foreign key with `op.create_foreign_key`, which Postgres accepts and SQLite
refuses outright -- SQLite cannot ALTER a table to add a constraint. It passed
every existing test, because the tests build their schema from
`metadata.create_all` and never run a migration at all.

So the schema was verified and the path that produces it in production was not.
These run the migrations end to end and then check the result matches what the
application code expects, which is the pair of assertions that makes a
migration trustworthy.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from studylink.db import make_engine
from studylink.schema import metadata

POSTGRES_URL = os.environ.get("STUDYLINK_TEST_POSTGRES_URL", "")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tables `create_all` builds that migrations legitimately do not, or vice
# versa, would show up as a difference here. There are none today; the set
# exists so that adding one is a deliberate edit rather than a silent drift.
EXPECTED_EXTRA: set[str] = set()


def alembic_config(url: str) -> Config:
    config = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(PROJECT_ROOT, "alembic"))
    return config


def migrate(url: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(alembic_config(url), "head")


def test_migrations_run_on_sqlite(tmp_path, monkeypatch):
    """The one that was broken. SQLite refuses ALTER for constraints, so any
    migration that adds a foreign key has to use batch mode."""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    migrate(url, monkeypatch)

    engine = make_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "decks" in tables and "cards" in tables and "card_reviews" in tables


def test_the_migrated_schema_matches_the_application_metadata(tmp_path, monkeypatch):
    """A migration that runs but produces the wrong shape is worse than one
    that fails: everything works until the first query hits the missing
    column."""
    url = f"sqlite:///{tmp_path / 'shape.db'}"
    migrate(url, monkeypatch)

    engine = make_engine(url)
    try:
        inspector = inspect(engine)
        migrated = set(inspector.get_table_names()) - {"alembic_version"}
        expected = set(metadata.tables)
        assert expected - migrated == EXPECTED_EXTRA, "migrations are missing a table"

        for table in sorted(expected & migrated):
            columns = {c["name"] for c in inspector.get_columns(table)}
            declared = {c.name for c in metadata.tables[table].columns}
            assert declared <= columns, f"{table} is missing {declared - columns}"
    finally:
        engine.dispose()


def test_a_migrated_database_can_be_used(tmp_path, monkeypatch):
    """Round-trips a note through the real code against a migrated schema,
    since "the columns exist" and "the app works" are different claims."""
    url = f"sqlite:///{tmp_path / 'usable.db'}"
    migrate(url, monkeypatch)

    from studylink import outline, store
    from studylink.db import connect

    conn = connect(url)
    try:
        user_id = store.create_user(conn, email="migrated@school.edu")
        note_id = store.create_note(conn, "Lecture", "a :: b", user_id=user_id)
        result = store.sync_note_cards(
            conn, user_id, note_id, outline.to_cards("a :: b"), title="Lecture"
        )
        assert result["added"] == 1
        assert len(store.list_cards(conn, user_id)) == 1
    finally:
        conn.close()


@pytest.mark.skipif(not POSTGRES_URL, reason="set STUDYLINK_TEST_POSTGRES_URL")
def test_migrations_run_on_postgres(monkeypatch):
    """Same migrations, the backend production actually uses."""
    engine = make_engine(POSTGRES_URL)
    try:
        with engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()

    migrate(POSTGRES_URL, monkeypatch)

    engine = make_engine(POSTGRES_URL)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"decks", "cards", "card_reviews"} <= tables
