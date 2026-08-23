"""A failed model call has to arrive as a sentence, not a 500.

This file exists because of a bug that reached a real user. Every Anthropic
call site handled "no credentials configured" carefully and handled nothing
else at all, so an API key with an empty credit balance -- the state every new
key starts in -- produced an unhandled exception and a bare 500 in the browser.

The failure was recoverable in about a minute, and the app said nothing that
would let anyone know that.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from studylink import cards as cards_module
from studylink import llm


def auth(token):
    return {"Authorization": f"Bearer {token}"}


NOTE_BODY = (
    "Gradient descent minimises a loss function by stepping downhill. "
    "The learning rate alpha controls the size of each step. "
    "Too large an alpha overshoots the minimum and diverges."
)


def api_error(message: str, status: int = 400) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cls = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    return cls(
        message=message, response=httpx.Response(status, request=request), body=None
    )


class FailingClient:
    """An Anthropic client whose every call fails the same way."""

    def __init__(self, exc):
        outer = self

        class Messages:
            def create(self, **kwargs):
                raise outer.exc

        self.exc = exc
        self.messages = Messages()


# ------------------------------------------------------------------ explain


def test_no_credit_says_where_to_add_credit():
    """The most common first failure, and the one worth naming exactly."""
    text = llm.explain(
        api_error(
            "Your credit balance is too low to access the Anthropic API. Please "
            "go to Plans & Billing to upgrade or purchase credits."
        )
    )
    assert "out of credit" in text
    assert llm.CONSOLE_BILLING in text
    # And the free route, since that is the actual answer for most students.
    assert "::" in text


def test_a_rejected_key_says_so_rather_than_blaming_credit():
    text = llm.explain(api_error("invalid x-api-key", status=401))
    assert "rejected that API key" in text
    assert "credit" not in text.lower()


def test_rate_limits_and_overloads_say_to_wait():
    assert "rate-limiting" in llm.explain(api_error("rate_limit_error", status=429))
    assert "overloaded" in llm.explain(api_error("Overloaded", status=529))


def test_an_unrecognised_failure_keeps_the_upstream_text():
    """Better a strange message than a generic one -- it names what happened,
    and there is nothing private in an error about the caller's own account."""
    text = llm.explain(RuntimeError("the tunnel collapsed"))
    assert "the tunnel collapsed" in text


def test_an_empty_message_still_names_the_exception():
    assert "TimeoutError" in llm.explain(TimeoutError())


def test_upstream_reraises_as_the_callers_own_type():
    with pytest.raises(cards_module.GenerationUnavailable) as caught:
        with llm.upstream(cards_module.GenerationUnavailable):
            raise api_error("Your credit balance is too low to access the API.")
    assert "out of credit" in str(caught.value)


def test_upstream_does_not_swallow_a_keyboard_interrupt():
    """Catching Exception rather than BaseException is deliberate: a worker
    being stopped should stop, not report a failed model call."""
    with pytest.raises(KeyboardInterrupt):
        with llm.upstream(cards_module.GenerationUnavailable):
            raise KeyboardInterrupt


def test_upstream_passes_success_through_untouched():
    with llm.upstream(cards_module.GenerationUnavailable):
        value = 1 + 1
    assert value == 2


# ------------------------------------------------------ through the endpoint


def test_generating_cards_without_credit_is_a_503_not_a_500(client, signup):
    """The bug, end to end. Before the fix this raised out of the endpoint."""
    token = signup(client)
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    failing = FailingClient(
        api_error("Your credit balance is too low to access the Anthropic API.")
    )
    real = cards_module.CardWriter
    import studylink.service as service_module

    service_module.cards_module.CardWriter = lambda *a, **k: real(client=failing)
    try:
        response = client.post(
            "/decks", json={"note_id": note["id"]}, headers=auth(token)
        )
    finally:
        service_module.cards_module.CardWriter = real

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "out of credit" in detail
    assert llm.CONSOLE_BILLING in detail


def test_a_failed_call_is_not_billed_to_the_student(client, signup):
    """A request that produced no cards must not spend the monthly allowance."""
    token = signup(client)
    note = client.post(
        "/notes", json={"title": "Optimisation", "body": NOTE_BODY}, headers=auth(token)
    ).json()

    failing = FailingClient(api_error("Overloaded", status=529))
    real = cards_module.CardWriter
    import studylink.service as service_module

    service_module.cards_module.CardWriter = lambda *a, **k: real(client=failing)
    try:
        client.post("/decks", json={"note_id": note["id"]}, headers=auth(token))
    finally:
        service_module.cards_module.CardWriter = real

    spend = client.get("/usage", headers=auth(token)).json()
    assert spend["calls"] == 0 and spend["cost_usd"] == 0
