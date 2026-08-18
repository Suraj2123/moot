"""LLM cost accounting and the per-user budget."""

from __future__ import annotations

from datetime import timedelta

import pytest

from studylink import store, usage
from studylink.schema import llm_usage


@pytest.fixture
def alice(conn):
    return store.create_user(conn, email="alice@school.edu")


def test_cost_is_computed_from_the_price_table():
    # 1M input + 1M output on opus-5 = $5 + $25.
    assert usage.cost_micros("claude-opus-5", 1_000_000, 1_000_000) == 30_000_000


def test_cost_rounds_up(monkeypatch):
    """Rounding to nearest lets a great many tiny calls sum to less than they
    actually cost.

    Needs a price that is a fractional number of micros per token: at $5 per
    million, one token is exactly 5 micros and there is nothing to round.
    """
    monkeypatch.setitem(usage.PRICES, "fractional", {"input": 1.5, "output": 0.0})

    assert usage.cost_micros("fractional", 1, 0) == 2  # 1.5 micros, rounded up
    assert usage.cost_micros("fractional", 2, 0) == 3  # 3.0 micros, exact
    assert usage.cost_micros("claude-opus-5", 0, 0) == 0


def test_an_unknown_model_is_priced_at_the_most_expensive_rate():
    """Failing safe here means failing expensive: an unknown model must count
    against the budget rather than slip through it."""
    unknown = usage.cost_micros("some-new-model", 1_000_000, 1_000_000)
    cheapest = usage.cost_micros("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert unknown > cheapest


def test_negative_token_counts_cannot_create_credit(conn, alice):
    """A bad usage payload must not refund the budget."""
    assert usage.cost_micros("claude-opus-5", -1_000_000, -1_000_000) == 0
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", -5, -5)
    assert usage.spend_since(conn, alice).micros == 0


def test_recording_accumulates(conn, alice):
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 1000, 500)
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 2000, 800)

    spend = usage.spend_since(conn, alice)
    assert spend.calls == 2
    assert spend.input_tokens == 3000
    assert spend.output_tokens == 1300
    assert spend.micros > 0


def test_spend_is_per_user(conn, alice):
    bob = store.create_user(conn, email="bob@school.edu")
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 100_000, 100_000)

    assert usage.spend_since(conn, alice).calls == 1
    assert usage.spend_since(conn, bob).calls == 0
    assert usage.spend_since(conn, bob).micros == 0


def test_spend_respects_the_window(conn, alice):
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 100_000, 100_000)

    conn.execute(
        llm_usage.update().values(created_at=usage._now() - timedelta(days=60))
    )
    conn.commit()

    assert usage.spend_since(conn, alice).calls == 0


def test_the_budget_blocks_once_spent(conn, alice, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.10")

    usage.check_budget(conn, alice)  # fine so far

    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    with pytest.raises(usage.BudgetExceeded):
        usage.check_budget(conn, alice)


def test_the_budget_message_says_where_you_stand(conn, alice, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.10")
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    with pytest.raises(usage.BudgetExceeded) as caught:
        usage.check_budget(conn, alice)

    assert "$0.10" in str(caught.value)


def test_one_users_spend_does_not_block_another(conn, alice, monkeypatch):
    bob = store.create_user(conn, email="bob@school.edu")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.10")

    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    with pytest.raises(usage.BudgetExceeded):
        usage.check_budget(conn, alice)
    usage.check_budget(conn, bob)  # must not raise


def test_a_zero_budget_disables_the_cap(conn, alice, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0")
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 10_000_000, 10_000_000)
    usage.check_budget(conn, alice)  # must not raise


def test_a_malformed_budget_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "not-a-number")
    assert usage.budget_micros() == usage.DEFAULT_MONTHLY_BUDGET_MICROS


def test_remaining_never_goes_negative(conn, alice, monkeypatch):
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.01")
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    assert usage.remaining_micros(conn, alice) == 0


def test_deleting_a_user_takes_their_ledger(conn, alice):
    usage.record(conn, alice, usage.KIND_CHAT, "claude-opus-5", 100, 100)
    conn.execute(store.users.delete().where(store.users.c.id == alice))
    conn.commit()

    assert conn.execute(llm_usage.select()).first() is None
