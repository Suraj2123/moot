"""Startup configuration checks.

Each of these guards a failure that is otherwise silent: the app starts, works
in testing, and loses data or leaks access later. The tests are mostly about the
production/development split, because a check that blocks local development gets
disabled and then protects nothing.
"""

from __future__ import annotations

import pytest

from studylink import preflight

GOOD = {
    "STUDYLINK_ENV": "production",
    "DATABASE_URL": "postgresql+psycopg2://user:pw@db/studylink",
    "STUDYLINK_SECRET_KEY": "v1:c29tZS1yZWFsLTMyLWJ5dGUta2V5LWhlcmUtb2s=",
    "ANTHROPIC_API_KEY": "sk-ant-whatever",
    "TRUST_PROXY_HEADERS": "1",
}


def test_a_complete_production_config_passes():
    findings = preflight.check(GOOD)
    assert findings.ok, findings.report()
    assert findings.errors == []


def test_development_never_blocks_on_missing_production_config():
    """A check that stops local work gets disabled, and then guards nothing."""
    findings = preflight.check({"STUDYLINK_ENV": "development"})

    assert findings.ok
    assert findings.warnings, "it should still say something"


def test_sqlite_in_production_is_an_error():
    """It works right up until the deploy that discards every note."""
    findings = preflight.check({**GOOD, "DATABASE_URL": "sqlite:///data/studylink.db"})
    assert not findings.ok
    assert any("SQLite" in e for e in findings.errors)


def test_no_database_url_in_production_is_an_error():
    config = {k: v for k, v in GOOD.items() if k != "DATABASE_URL"}
    findings = preflight.check(config)
    assert not findings.ok


def test_a_missing_secret_key_in_production_is_an_error():
    config = {k: v for k, v in GOOD.items() if k != "STUDYLINK_SECRET_KEY"}
    findings = preflight.check(config)
    assert not findings.ok
    assert any("SECRET_KEY" in e for e in findings.errors)


def test_the_public_ci_key_is_rejected_even_in_development():
    """It is in the repo, in CI, and in the docs. Anyone can decrypt with it,
    so this one is fatal regardless of environment."""
    for env in ["development", "production"]:
        findings = preflight.check({
            "STUDYLINK_ENV": env,
            "STUDYLINK_SECRET_KEY": "v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        })
        assert not findings.ok, env
        assert any("public" in e for e in findings.errors)


def test_wildcard_cors_is_an_error():
    """It lets any page on the internet call the API with a user's token."""
    findings = preflight.check({**GOOD, "CORS_ALLOW_ORIGINS": "*"})
    assert not findings.ok
    assert any("CORS" in e for e in findings.errors)


def test_named_cors_origins_are_fine():
    findings = preflight.check({**GOOD, "CORS_ALLOW_ORIGINS": "https://studylink.app"})
    assert findings.ok


def test_missing_proxy_header_setting_warns_but_does_not_block():
    """Getting this wrong in either direction is bad, so it is the deployer's
    call -- but they should be told."""
    config = {k: v for k, v in GOOD.items() if k != "TRUST_PROXY_HEADERS"}
    findings = preflight.check(config)

    assert findings.ok
    assert any("TRUST_PROXY_HEADERS" in w for w in findings.warnings)


def test_a_disabled_spend_cap_warns():
    findings = preflight.check({**GOOD, "LLM_MONTHLY_BUDGET_USD": "0"})
    assert findings.ok
    assert any("spend cap" in w for w in findings.warnings)


def test_run_raises_in_production_and_not_otherwise(monkeypatch):
    monkeypatch.setenv("STUDYLINK_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STUDYLINK_SECRET_KEY", raising=False)

    with pytest.raises(preflight.ConfigurationError):
        preflight.run()

    monkeypatch.setenv("STUDYLINK_ENV", "development")
    assert preflight.run().ok


def test_the_report_names_what_to_do():
    """An error that does not say how to fix it just moves the confusion."""
    findings = preflight.check({k: v for k, v in GOOD.items() if k != "STUDYLINK_SECRET_KEY"})
    assert "STUDYLINK_SECRET_KEY" in findings.report()
    assert "Canvas" in findings.report()


# ------------------------------------------------------------------- probes


def test_liveness_touches_nothing(client):
    """A liveness probe that queries the database turns a brief outage into a
    restart loop, because the platform kills a web process that is fine."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_liveness_needs_no_credentials(client):
    assert client.get("/healthz").status_code == 200


def test_readiness_reports_dependencies(client):
    response = client.get("/readyz")
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is True
    assert body["database"] == "ok"
    assert "migrations" in body
    assert "static" in body


def test_readiness_reports_no_user_data(client, signup):
    """It is unauthenticated, so it must say nothing about what is stored."""
    token = signup(client)
    client.post("/notes", headers={"Authorization": f"Bearer {token}"},
                json={"title": "Private", "body": "ALICE-SECRET-TOKEN"})

    body = client.get("/readyz").text
    assert "ALICE-SECRET-TOKEN" not in body
    assert "alice@school.edu" not in body


def test_readiness_fails_when_the_database_is_gone(client, monkeypatch):
    """503 is what tells a load balancer to stop sending traffic here."""
    import studylink.api as api_module

    def broken():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(api_module, "engine", broken)
    response = client.get("/readyz")

    # 503 specifically, with a body: a load balancer needs to be told to stop
    # sending traffic, and a 500 traceback is a crash report rather than a
    # health signal.
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "error" in response.json()["database"]
