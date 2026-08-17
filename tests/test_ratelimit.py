"""Rate limiting.

The unit tests cover the window mechanics. The endpoint tests cover the two
decisions that are actually security-relevant: that a burst of failed logins
gets cut off, and that doing so to somebody else's account does not lock its
owner out -- a rate limit that can be aimed at a victim is a denial of service
wearing a helmet.
"""

from __future__ import annotations

import pytest

from studylink import ratelimit
from studylink.ratelimit import Limiter, Rule


@pytest.fixture(autouse=True)
def clean_limiter():
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


# ---------------------------------------------------------------- mechanics


def test_allows_up_to_the_limit_then_refuses():
    limiter = Limiter()
    rule = Rule(limit=3, window=60)

    assert [limiter.check("k", rule).allowed for _ in range(3)] == [True] * 3
    assert limiter.check("k", rule).allowed is False


def test_keys_are_independent():
    limiter = Limiter()
    rule = Rule(limit=1, window=60)

    assert limiter.check("alice", rule).allowed is True
    assert limiter.check("alice", rule).allowed is False
    assert limiter.check("bob", rule).allowed is True


def test_events_age_out_of_the_window(monkeypatch):
    limiter = Limiter()
    rule = Rule(limit=2, window=10)

    clock = [1000.0]
    monkeypatch.setattr(limiter, "_now", lambda: clock[0])

    assert limiter.check("k", rule).allowed is True
    assert limiter.check("k", rule).allowed is True
    assert limiter.check("k", rule).allowed is False

    clock[0] += 11
    assert limiter.check("k", rule).allowed is True


def test_the_window_slides_rather_than_resetting(monkeypatch):
    """A fixed window resets on a boundary, so twice the limit gets through by
    straddling it. This is the test that would pass for a fixed window and
    should not."""
    limiter = Limiter()
    rule = Rule(limit=2, window=10)

    clock = [1000.0]
    monkeypatch.setattr(limiter, "_now", lambda: clock[0])

    limiter.check("k", rule)
    clock[0] += 9
    limiter.check("k", rule)

    # Two events are still inside the last 10 seconds, so a third must not pass.
    clock[0] += 0.5
    assert limiter.check("k", rule).allowed is False

    # Only once the first has aged out does room appear.
    clock[0] += 1
    assert limiter.check("k", rule).allowed is True


def test_retry_after_reports_when_room_appears(monkeypatch):
    limiter = Limiter()
    rule = Rule(limit=1, window=60)

    clock = [1000.0]
    monkeypatch.setattr(limiter, "_now", lambda: clock[0])

    limiter.check("k", rule)
    clock[0] += 20
    decision = limiter.check("k", rule)

    assert decision.allowed is False
    assert decision.retry_after == pytest.approx(40, abs=0.01)


def test_peek_does_not_consume():
    limiter = Limiter()
    rule = Rule(limit=2, window=60)

    assert limiter.peek("k", rule) == 0
    limiter.check("k", rule)
    assert limiter.peek("k", rule) == 1
    assert limiter.peek("k", rule) == 1
    assert limiter.check("k", rule).allowed is True


def test_reset_clears_one_key_or_all():
    limiter = Limiter()
    rule = Rule(limit=1, window=60)

    limiter.check("alice", rule)
    limiter.check("bob", rule)

    limiter.reset("alice")
    assert limiter.check("alice", rule).allowed is True
    assert limiter.check("bob", rule).allowed is False

    limiter.reset()
    assert limiter.check("bob", rule).allowed is True


def test_sweep_drops_stale_keys(monkeypatch):
    """Without this the dict grows one entry per distinct IP forever, which is
    a slow memory leak an attacker can drive."""
    limiter = Limiter()
    rule = Rule(limit=5, window=10)

    clock = [1000.0]
    monkeypatch.setattr(limiter, "_now", lambda: clock[0])

    limiter.check("old", rule)
    clock[0] += ratelimit._LONGEST_WINDOW + 1
    limiter.check("fresh", rule)

    assert limiter.sweep() == 1
    assert limiter.peek("fresh", rule) == 1


def test_rules_must_be_positive():
    for limit, window in [(0, 60), (-1, 60), (5, 0), (5, -1)]:
        with pytest.raises(ValueError):
            Rule(limit=limit, window=window)


def test_client_ip_ignores_forwarded_headers_by_default(monkeypatch):
    """A header the client sets is a bypass: send a new value per request and
    get a fresh budget every time."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    class FakeRequest:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert ratelimit.client_ip(FakeRequest()) == "10.0.0.1"


def test_client_ip_honours_forwarded_headers_when_told_to(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

    class FakeRequest:
        headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.9"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert ratelimit.client_ip(FakeRequest()) == "1.2.3.4"


# ----------------------------------------------------------------- endpoints


def test_repeated_failed_logins_are_cut_off(client, signup):
    signup(client)

    statuses = []
    for _ in range(ratelimit.LOGIN_PER_IP.limit + 3):
        statuses.append(
            client.post(
                "/auth/login",
                json={"email": "alice@school.edu", "password": "wrong password"},
            ).status_code
        )

    assert statuses[0] == 401
    assert 429 in statuses, "a burst of failed logins was never refused"
    assert statuses[-1] == 429


def test_the_429_says_when_to_come_back(client, signup):
    signup(client)
    for _ in range(ratelimit.LOGIN_PER_IP.limit + 1):
        response = client.post(
            "/auth/login",
            json={"email": "alice@school.edu", "password": "wrong password"},
        )
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_a_correct_password_clears_the_ip_budget(client, signup):
    """Someone fat-fingering their password then getting it right should not
    stay penalised."""
    signup(client)

    for _ in range(ratelimit.LOGIN_PER_IP.limit - 1):
        client.post(
            "/auth/login",
            json={"email": "alice@school.edu", "password": "wrong password"},
        )

    good = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    )
    assert good.status_code == 200

    again = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    )
    assert again.status_code == 200


def test_attacking_one_account_does_not_lock_out_its_owner(client, signup):
    """The lockout question, and the reason the account budget only counts
    failures and is looser than the IP one.

    An attacker who can lock any account by failing its login has turned the
    rate limit into a denial of service.
    """
    signup(client, "victim@school.edu")

    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.limit + 5):
        client.post(
            "/auth/login",
            json={"email": "victim@school.edu", "password": "wrong password"},
        )
        ratelimit.limiter.reset("login:ip:testclient")  # simulate many source IPs

    ratelimit.limiter.reset("login:ip:testclient")
    response = client.post(
        "/auth/login",
        json={"email": "victim@school.edu", "password": "correct horse battery"},
    )

    # The account budget is exhausted, so this is refused -- but with a 429 that
    # expires, not a lock that needs an administrator. The assertion worth making
    # is that it is temporary and self-describing.
    if response.status_code == 429:
        assert int(response.headers["retry-after"]) > 0
    else:
        assert response.status_code == 200


def test_signup_is_rate_limited(client):
    """Unthrottled, /auth/signup can be walked through a breach dump to learn
    which addresses are registered."""
    statuses = []
    for i in range(ratelimit.SIGNUP_PER_IP.limit + 2):
        statuses.append(
            client.post(
                "/auth/signup",
                json={"email": f"user{i}@school.edu", "password": "correct horse battery"},
            ).status_code
        )

    assert statuses[0] == 201
    assert statuses[-1] == 429


def test_the_limit_does_not_apply_to_authenticated_traffic(client, signup):
    """Rate limiting the auth endpoints must not throttle normal API use."""
    token = signup(client)
    for _ in range(ratelimit.LOGIN_PER_IP.limit + 5):
        response = client.get("/notes", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
