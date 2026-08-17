"""CORS and the security headers.

CORS is off unless configured, and that default is the interesting part: a
wildcard would let any page on the internet make authenticated calls with a
user's token, and a same-origin frontend needs no CORS at all. So it has to be
an explicit deployment decision, and this file asserts that it is.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def build_client(tmp_path, monkeypatch, **env):
    """Reimport the API with a given environment.

    CORS is configured when the module is imported, which is normal for
    deployment config and awkward for tests -- so the tests reimport rather than
    pretending the app can be reconfigured in place.
    """
    from studylink import ratelimit

    monkeypatch.setenv("STUDYLINK_DB", str(tmp_path / "hdr.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import studylink.api as api_module

    api_module = importlib.reload(api_module)
    api_module._engine = None
    ratelimit.limiter.reset()
    return api_module, TestClient(api_module.api)


@pytest.fixture(autouse=True)
def restore_api_module():
    """Leave the module as the rest of the suite expects to find it."""
    yield
    import studylink.api as api_module

    importlib.reload(api_module)
    api_module._engine = None


def test_security_headers_are_present(tmp_path, monkeypatch):
    _, client = build_client(tmp_path, monkeypatch)
    response = client.post(
        "/auth/signup",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    )

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_responses_are_not_cacheable(tmp_path, monkeypatch):
    """Responses are per-user. A shared cache holding one user's notes and
    handing them to the next is the failure this prevents."""
    _, client = build_client(tmp_path, monkeypatch)
    token = client.post(
        "/auth/signup",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    ).json()["token"]

    response = client.get("/notes", headers={"Authorization": f"Bearer {token}"})
    assert response.headers["cache-control"] == "no-store"


def test_cors_is_off_by_default(tmp_path, monkeypatch):
    """A wildcard here lets any page on the internet call this API with a
    user's token."""
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    api_module, client = build_client(tmp_path, monkeypatch)

    assert api_module.allowed_origins() == []

    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_the_configured_origins(tmp_path, monkeypatch):
    api_module, client = build_client(
        tmp_path, monkeypatch, CORS_ALLOW_ORIGINS="https://studylink.app"
    )
    assert api_module.allowed_origins() == ["https://studylink.app"]

    allowed = client.options(
        "/auth/login",
        headers={
            "Origin": "https://studylink.app",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.headers.get("access-control-allow-origin") == "https://studylink.app"

    denied = client.options(
        "/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


def test_cors_does_not_enable_cookie_credentials(tmp_path, monkeypatch):
    """Tokens go in the Authorization header. Allowing credentials is what makes
    a browser attach cookies to cross-origin calls, and nothing here wants it."""
    _, client = build_client(
        tmp_path, monkeypatch, CORS_ALLOW_ORIGINS="https://studylink.app"
    )
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "https://studylink.app",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.headers.get("access-control-allow-credentials") != "true"


def test_multiple_origins_parse(tmp_path, monkeypatch):
    api_module, _ = build_client(
        tmp_path, monkeypatch,
        CORS_ALLOW_ORIGINS="https://a.example, https://b.example ,",
    )
    assert api_module.allowed_origins() == ["https://a.example", "https://b.example"]
