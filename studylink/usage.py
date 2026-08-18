"""What the LLM costs, per user, and the cap that stops it running away.

An LLM in the product means spend that scales with use by someone who is not
paying for it. Two things follow, and only the second is interesting.

The easy one: record every call. A ledger rather than a counter, because the
questions worth asking are "what has this account spent this month" and "what
did that conversation cost", and a running total answers neither.

The one with a decision in it: **the cap is checked before the call and the cost
is recorded after.** That ordering means a user can always overshoot their limit
by one message, because the size of a reply is not knowable until it exists. The
alternative -- reserving an estimate up front and reconciling afterwards -- is
materially more machinery for a bound that is approximate either way. So the
overshoot is accepted, bounded by max_tokens, and stated rather than hidden.

Prices are hardcoded and will go stale. That is deliberate: fetching them at
runtime puts a network call in front of a budget check, and a budget check that
can fail open is worse than one that is occasionally out of date. `PRICES` is
one dict to edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Connection, func, select

from .db import transaction
from .schema import llm_usage

# US dollars per million tokens. Update alongside the model list.
PRICES = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}
# Used when a model is not in the table. Deliberately the most expensive entry,
# so an unknown model over-counts against the budget rather than slipping
# through it -- failing safe means failing expensive here.
FALLBACK_PRICE = {"input": 5.00, "output": 25.00}

KIND_CHAT = "chat"
KIND_WORK_SESSION = "work_session"
KIND_JUDGE = "judge"

# Per user, per rolling 30 days. Generous for a student revising and low enough
# that a runaway script is a bad afternoon rather than a bad month.
DEFAULT_MONTHLY_BUDGET_MICROS = 2_000_000  # $2.00


class BudgetExceeded(RuntimeError):
    """This account has spent its allowance. Message is safe to show."""


@dataclass(frozen=True)
class Spend:
    micros: int
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def dollars(self) -> float:
        return self.micros / 1_000_000

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.dollars, 6),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cost_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    """Cost of one call in micro-dollars, rounded up.

    Rounded up rather than to nearest so a great many tiny calls cannot sum to
    less than they cost.
    """
    price = PRICES.get(model, FALLBACK_PRICE)
    dollars = (
        (max(0, input_tokens) / 1_000_000) * price["input"]
        + (max(0, output_tokens) / 1_000_000) * price["output"]
    )
    micros = dollars * 1_000_000
    return int(micros) + (1 if micros > int(micros) else 0)


def record(
    conn: Connection,
    user_id: int,
    kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> int:
    """Write one call to the ledger. Returns its cost in micros."""
    micros = cost_micros(model, input_tokens, output_tokens)
    with transaction(conn):
        conn.execute(
            llm_usage.insert().values(
                user_id=int(user_id),
                kind=kind,
                model=model,
                input_tokens=max(0, int(input_tokens)),
                output_tokens=max(0, int(output_tokens)),
                cost_micros=micros,
                created_at=_now(),
            )
        )
    return micros


def spend_since(
    conn: Connection, user_id: int, since: Optional[datetime] = None
) -> Spend:
    """What one account has spent since `since` (default: 30 days ago)."""
    cutoff = since or (_now() - timedelta(days=30))
    row = conn.execute(
        select(
            func.coalesce(func.sum(llm_usage.c.cost_micros), 0),
            func.count(),
            func.coalesce(func.sum(llm_usage.c.input_tokens), 0),
            func.coalesce(func.sum(llm_usage.c.output_tokens), 0),
        ).where(
            llm_usage.c.user_id == int(user_id),
            llm_usage.c.created_at >= cutoff,
        )
    ).first()

    return Spend(
        micros=int(row[0] or 0),
        calls=int(row[1] or 0),
        input_tokens=int(row[2] or 0),
        output_tokens=int(row[3] or 0),
    )


def budget_micros() -> int:
    import os

    raw = os.environ.get("LLM_MONTHLY_BUDGET_USD", "").strip()
    if not raw:
        return DEFAULT_MONTHLY_BUDGET_MICROS
    try:
        return max(0, int(float(raw) * 1_000_000))
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET_MICROS


def check_budget(conn: Connection, user_id: int) -> Spend:
    """Raise if this account is out of allowance. Returns spend so far.

    Called before the request, so the reported spend excludes the call about to
    happen. See the module docstring for why one message of overshoot is
    accepted rather than engineered away.
    """
    spent = spend_since(conn, user_id)
    limit = budget_micros()
    if limit and spent.micros >= limit:
        raise BudgetExceeded(
            f"You have used this month's AI allowance "
            f"(${spent.dollars:.2f} of ${limit / 1_000_000:.2f}). "
            "It resets on a rolling 30-day window."
        )
    return spent


def remaining_micros(conn: Connection, user_id: int) -> int:
    return max(0, budget_micros() - spend_since(conn, user_id).micros)
