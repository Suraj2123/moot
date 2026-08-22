"""Scoring: what has been learned, and what needs practice.

Pure arithmetic over review history, so these are exact assertions. The ones
that matter are about the ranking: raw accuracy is the obvious choice and it
sends the student to revise the wrong card.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from studylink import progress


def card(attempts=0, correct=0, lapses=0, interval=0, **extra):
    return {
        "id": extra.get("id", 1),
        "front": extra.get("front", "Q"),
        "back": extra.get("back", "A"),
        "attempts": attempts,
        "correct": correct,
        "lapses": lapses,
        "interval_days": interval,
        **extra,
    }


# ----------------------------------------------------------------- ranking


def test_repeated_failure_outranks_a_single_miss():
    """The whole reason this is not raw accuracy: both are 0% by that measure,
    which makes them indistinguishable and fills the list with cards the
    student has barely met."""
    seen_once = card(id=1, attempts=1, correct=0, front="seen once")
    keeps_failing = card(id=2, attempts=10, correct=0, front="keeps failing")

    ranked = progress.needs_practice([seen_once, keeps_failing])

    assert [c["front"] for c in ranked] == ["keeps failing", "seen once"]


def test_a_single_miss_still_appears():
    """It is not noise to be filtered out -- the student did just get it
    wrong. It simply should not lead the list."""
    ranked = progress.needs_practice([card(attempts=1, correct=0)])
    assert len(ranked) == 1


def test_a_card_never_seen_is_not_weak():
    """It is new. Mixing the two buries real problems under everything the
    student has not started."""
    assert progress.needs_practice([card(attempts=0)]) == []


def test_a_card_answered_reliably_is_not_listed():
    assert progress.needs_practice([card(attempts=20, correct=20)]) == []


def test_lapses_break_ties():
    """Same rate, but one has been learned and forgotten -- that one is the
    more fragile."""
    steady = card(id=1, attempts=10, correct=5, lapses=0, front="steady")
    fragile = card(id=2, attempts=10, correct=5, lapses=4, front="fragile")

    ranked = progress.needs_practice([steady, fragile])

    assert [c["front"] for c in ranked] == ["fragile", "steady"]


def test_the_list_is_capped():
    weak = [card(id=i, attempts=10, correct=1) for i in range(50)]
    assert len(progress.needs_practice(weak, limit=5)) == 5


def test_confidence_is_reported_not_just_used():
    ranked = progress.needs_practice([card(attempts=10, correct=3)])
    assert 0 < ranked[0]["confidence"] < 1


# -------------------------------------------------------- confidence bound


def test_evidence_moves_the_estimate_away_from_the_prior():
    """One attempt barely moves it; a hundred should dominate."""
    assert progress.smoothed_rate(0, 1) == pytest.approx(0.4)
    assert progress.smoothed_rate(0, 100) < 0.03


def test_a_perfect_record_is_still_not_certainty():
    assert progress.smoothed_rate(20, 20) < 1.0


def test_no_attempts_returns_the_prior_not_zero():
    """Zero would rank an unseen card as the worst thing the student knows."""
    assert progress.smoothed_rate(0, 0) == progress.PRIOR_RATE


def test_the_estimate_stays_in_range():
    for correct, attempts in [(0, 1), (1, 1), (7, 9), (99, 100), (0, 0)]:
        assert 0.0 <= progress.smoothed_rate(correct, attempts) <= 1.0


def test_more_failures_always_score_worse():
    """Monotonic in the thing it claims to measure."""
    rates = [progress.smoothed_rate(0, n) for n in (1, 5, 10, 40)]
    assert rates == sorted(rates, reverse=True)


# ------------------------------------------------------------------ states


def test_states_split_new_learning_and_mastered():
    assert progress.state_of(card(attempts=0)) == "new"
    assert progress.state_of(card(attempts=3, interval=5)) == "learning"
    assert progress.state_of(card(attempts=9, interval=40)) == "mastered"


def test_mastery_needs_attempts_not_just_an_interval():
    """A card with no history is new whatever its schedule column says."""
    assert progress.state_of(card(attempts=0, interval=99)) == "new"


# ----------------------------------------------------------------- streaks


def days_back(*offsets, reviews=1):
    today = date.today()
    return [
        {"day": (today - timedelta(days=n)).isoformat(), "reviews": reviews}
        for n in offsets
    ]


def test_consecutive_days_count():
    assert progress.streak(days_back(0, 1, 2)) == 3


def test_a_gap_ends_the_streak():
    assert progress.streak(days_back(0, 1, 3, 4)) == 2


def test_studying_yesterday_but_not_yet_today_still_counts():
    """Telling someone at 9am that they have lost a fortnight is both wrong and
    discouraging."""
    assert progress.streak(days_back(1, 2, 3)) == 3


def test_a_missed_day_before_yesterday_is_a_broken_streak():
    assert progress.streak(days_back(2, 3, 4)) == 0


def test_no_activity_is_no_streak():
    assert progress.streak([]) == 0
    assert progress.streak([{"day": date.today().isoformat(), "reviews": 0}]) == 0


# ---------------------------------------------------------------- summary


def test_the_summary_counts_each_state():
    summary = progress.summarise(
        [
            card(id=1, attempts=0),
            card(id=2, attempts=5, correct=3, interval=4),
            card(id=3, attempts=9, correct=9, interval=60),
        ],
        days_back(0, 1),
    )
    assert summary["cards"] == 3
    assert (summary["new"], summary["learning"], summary["mastered"]) == (1, 1, 1)


def test_overall_accuracy_is_across_attempts_not_cards():
    summary = progress.summarise(
        [card(id=1, attempts=10, correct=5), card(id=2, attempts=2, correct=2)],
        [],
    )
    assert summary["attempts"] == 12
    assert summary["correct"] == 7
    assert summary["accuracy"] == round(7 / 12, 3)


def test_accuracy_is_none_rather_than_zero_when_nothing_was_attempted():
    """Zero would read as "you got everything wrong"."""
    assert progress.summarise([card(attempts=0)], [])["accuracy"] is None


def test_an_empty_account_summarises_without_crashing():
    summary = progress.summarise([], [])
    assert summary["cards"] == 0
    assert summary["streak_days"] == 0
