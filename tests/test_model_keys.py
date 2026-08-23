"""A student's own model key: stored encrypted, never readable, never capped.

The point of this feature is that "free" stays true. One thing in this app
costs money, and the operator paying for it is what eventually forces every
free product to stop being free. A user who brings their own key pays their
provider directly.

Which makes the tests here two separate obligations. The key must never come
back out -- not through an endpoint, not in a log line, not in an error. And
the allowance must genuinely stop applying, or the feature is decorative.
"""

from __future__ import annotations

import logging

import pytest

from studylink import modelkeys, vault


def auth(token):
    return {"Authorization": f"Bearer {token}"}


KEY = "sk-ant-api03-thisisnotarealkeyatall-000111222333"
NOTE_BODY = (
    "Gradient descent minimises a loss function by stepping downhill. "
    "The learning rate alpha controls the size of each step. "
    "Too large an alpha overshoots the minimum and diverges."
)


@pytest.fixture
def vault_key(monkeypatch):
    """A deployment that can store secrets at all."""
    monkeypatch.setenv("STUDYLINK_SECRET_KEY", f"v1:{vault.generate_key().split(':', 1)[1]}")
    return True


# ------------------------------------------------------------------ storage


def test_a_stored_key_comes_back_only_to_the_caller_spending_it(conn, two_users, vault_key):
    modelkeys.save_key(conn, two_users["alice"], KEY)

    assert modelkeys.get_key(conn, two_users["alice"]) == KEY
    # And to nobody else. The vault's AAD binds the ciphertext to one user, so
    # this is a failed decryption rather than a missing WHERE clause.
    assert modelkeys.get_key(conn, two_users["bob"]) is None


def test_status_cannot_carry_a_key(conn, two_users, vault_key):
    """A dataclass with no field for a secret cannot leak one."""
    modelkeys.save_key(conn, two_users["alice"], KEY)
    status = modelkeys.get_status(conn, two_users["alice"])

    assert status.hint == KEY[-4:]
    assert KEY not in str(status.as_dict())
    assert not hasattr(status, "api_key")


def test_the_stored_row_is_not_the_key(conn, two_users, vault_key):
    from sqlalchemy import select

    from studylink.schema import model_credentials

    modelkeys.save_key(conn, two_users["alice"], KEY)
    stored = conn.execute(select(model_credentials.c.encrypted_key)).scalar()
    assert KEY not in stored


def test_saving_twice_replaces_rather_than_accumulates(conn, two_users, vault_key):
    modelkeys.save_key(conn, two_users["alice"], KEY)
    modelkeys.save_key(conn, two_users["alice"], KEY[:-4] + "9999")

    assert modelkeys.get_key(conn, two_users["alice"]).endswith("9999")
    assert modelkeys.get_status(conn, two_users["alice"]).hint == "9999"


def test_deleting_a_key_leaves_nothing_behind(conn, two_users, vault_key):
    modelkeys.save_key(conn, two_users["alice"], KEY)
    modelkeys.delete_key(conn, two_users["alice"])

    assert modelkeys.get_key(conn, two_users["alice"]) is None
    assert modelkeys.get_status(conn, two_users["alice"]) is None


def test_a_truncated_paste_is_refused_here_rather_than_by_the_provider(conn, two_users, vault_key):
    with pytest.raises(modelkeys.ModelKeyError, match="complete API key"):
        modelkeys.save_key(conn, two_users["alice"], "sk-ant-")


def test_a_deployment_with_no_secret_key_says_so(conn, two_users, monkeypatch):
    monkeypatch.delenv("STUDYLINK_SECRET_KEY", raising=False)
    with pytest.raises(modelkeys.ModelKeyError, match="STUDYLINK_SECRET_KEY"):
        modelkeys.save_key(conn, two_users["alice"], KEY)


def test_a_key_encrypted_under_a_lost_secret_reads_as_absent(conn, two_users, monkeypatch):
    """A rotation that lost the old secret, or a restored backup. "No key"
    sends the user to paste a new one, which is the only thing that fixes it."""
    monkeypatch.setenv("STUDYLINK_SECRET_KEY", vault.generate_key())
    modelkeys.save_key(conn, two_users["alice"], KEY)

    monkeypatch.setenv("STUDYLINK_SECRET_KEY", vault.generate_key())
    assert modelkeys.get_key(conn, two_users["alice"]) is None


def test_saving_a_key_writes_nothing_secret_to_the_log(conn, two_users, vault_key, caplog):
    with caplog.at_level(logging.DEBUG):
        modelkeys.save_key(conn, two_users["alice"], KEY)
        modelkeys.get_key(conn, two_users["alice"])
    assert KEY not in caplog.text


# ----------------------------------------------------------- through the API


def test_the_api_never_returns_the_key(client, signup, vault_key):
    token = signup(client)
    saved = client.post("/model-key", json={"api_key": KEY}, headers=auth(token))

    assert saved.status_code == 201
    assert KEY not in saved.text
    assert saved.json()["hint"] == KEY[-4:]

    status = client.get("/model-key", headers=auth(token))
    assert KEY not in status.text
    assert status.json()["connected"] is True


def test_one_users_key_is_invisible_to_another(client, signup, vault_key):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    client.post("/model-key", json={"api_key": KEY}, headers=auth(alice))

    assert client.get("/model-key", headers=auth(bob)).json()["connected"] is False


def test_removing_a_key_through_the_api(client, signup, vault_key):
    token = signup(client)
    client.post("/model-key", json={"api_key": KEY}, headers=auth(token))

    assert client.delete("/model-key", headers=auth(token)).status_code == 204
    assert client.get("/model-key", headers=auth(token)).json()["connected"] is False


def test_the_key_endpoints_need_a_session(client):
    assert client.get("/model-key").status_code == 401
    assert client.post("/model-key", json={"api_key": KEY}).status_code == 401
    assert client.delete("/model-key").status_code == 401


# ------------------------------------------------------------- the allowance


def test_a_users_own_key_is_used_for_generation(client, signup, vault_key, monkeypatch):
    """The whole feature, in one assertion: the call is made with their key."""
    from studylink import cards as cards_module
    from studylink import service as service_module

    token = signup(client)
    client.post("/model-key", json={"api_key": KEY}, headers=auth(token))
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    seen = {}
    real = cards_module.CardWriter

    def spy(*args, **kwargs):
        seen["api_key"] = kwargs.get("api_key")
        return real(client=_stub(), **{k: v for k, v in kwargs.items() if k != "api_key"})

    monkeypatch.setattr(service_module.cards_module, "CardWriter", spy)
    client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    assert seen["api_key"] == KEY


def test_without_a_key_the_environment_is_used(client, signup, vault_key, monkeypatch):
    from studylink import cards as cards_module
    from studylink import service as service_module

    token = signup(client)
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    seen = {}
    real = cards_module.CardWriter

    def spy(*args, **kwargs):
        seen["api_key"] = kwargs.get("api_key")
        return real(client=_stub(), **{k: v for k, v in kwargs.items() if k != "api_key"})

    monkeypatch.setattr(service_module.cards_module, "CardWriter", spy)
    client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    assert seen["api_key"] is None


def test_the_monthly_cap_does_not_apply_to_someone_paying_their_own_way(
    client, signup, vault_key, monkeypatch
):
    """The allowance caps what the operator pays for. A user spending their own
    key is not spending that, so there is nothing left for it to protect."""
    from studylink import cards as cards_module
    from studylink import service as service_module

    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.000001")
    token = signup(client)
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    real = cards_module.CardWriter
    monkeypatch.setattr(
        service_module.cards_module,
        "CardWriter",
        lambda *a, **k: real(client=_stub()),
    )

    # Spend the whole allowance first.
    client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    blocked = client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    assert blocked.status_code == 429

    client.post("/model-key", json={"api_key": KEY}, headers=auth(token))
    allowed = client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    assert allowed.status_code == 201


def test_spending_is_still_recorded_for_a_user_with_their_own_key(
    client, signup, vault_key, monkeypatch
):
    """Not capped is not the same as not measured. The ledger is how anyone
    finds out what a feature actually costs."""
    from studylink import cards as cards_module
    from studylink import service as service_module

    token = signup(client)
    client.post("/model-key", json={"api_key": KEY}, headers=auth(token))
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    real = cards_module.CardWriter
    monkeypatch.setattr(
        service_module.cards_module,
        "CardWriter",
        lambda *a, **k: real(client=_stub()),
    )
    client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))

    assert client.get("/usage", headers=auth(token)).json()["calls"] == 1


# -------------------------------------------------------------- the free page


def test_pricing_lists_the_free_path_and_the_one_paid_thing(client, signup):
    token = signup(client)
    body = client.get("/pricing", headers=auth(token)).json()

    assert any("::" in item for item in body["free"])
    assert any("prose" in item for item in body["paid"])
    assert body["own_key"] is False
    assert body["capped"] is True


def test_pricing_stops_claiming_a_cap_once_a_key_is_supplied(client, signup, vault_key):
    token = signup(client)
    client.post("/model-key", json={"api_key": KEY}, headers=auth(token))

    body = client.get("/pricing", headers=auth(token)).json()
    assert body["own_key"] is True
    assert body["capped"] is False


def _stub():
    """A client that returns two grounded cards, so generation succeeds."""
    import json

    payload = json.dumps({
        "cards": [
            {
                "front": "What does alpha control?",
                "back": "The step size",
                "evidence": "The learning rate alpha controls the size of each step.",
            }
        ]
    })

    class Message:
        content = [type("Block", (), {"text": payload})()]
        usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()

    class Messages:
        def create(self, **kwargs):
            return Message()

    return type("Client", (), {"messages": Messages()})()
