"""Ranking metrics.

Four numbers, because each one hides a different failure:

  precision@k  How much of what the student sees is actually useful. Falls when
               the retriever pads the list with junk.
  recall@k     How much of the useful material they were shown at all. Falls when
               a relevant note never surfaces -- the failure a student cannot
               detect from the UI, and the one that matters most here.
  MRR          How far down the list the first useful note sits. A system with
               good recall but bad MRR technically works and feels broken.
  nDCG@k       Recall and ordering together, discounted by position.

A note on the denominator for precision@k: when an assignment has fewer than k
relevant notes, precision@k is capped below 1.0 no matter how good the retriever
is. That is not a bug, but it means precision@k is only comparable *across
configurations on the same label set*, never as an absolute score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


def precision_at_k(ranked_ids: Sequence[int], relevant_ids: Iterable[int], k: int) -> float:
    if k <= 0:
        return 0.0
    relevant = set(relevant_ids)
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for item in top if item in relevant) / k


def recall_at_k(ranked_ids: Sequence[int], relevant_ids: Iterable[int], k: int) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    hits = sum(1 for item in ranked_ids[:k] if item in relevant)
    return hits / len(relevant)


def reciprocal_rank(ranked_ids: Sequence[int], relevant_ids: Iterable[int]) -> float:
    relevant = set(relevant_ids)
    for position, item in enumerate(ranked_ids, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def average_precision(ranked_ids: Sequence[int], relevant_ids: Iterable[int]) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for position, item in enumerate(ranked_ids, start=1):
        if item in relevant:
            hits += 1
            total += hits / position
    return total / len(relevant)


def ndcg_at_k(ranked_ids: Sequence[int], relevant_ids: Iterable[int], k: int) -> float:
    """Binary-gain nDCG: every relevant note is worth the same, position discounts it."""
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, item in enumerate(ranked_ids[:k], start=1)
        if item in relevant
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


@dataclass
class RankingMetrics:
    precision_at_k: float
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    average_precision: float
    k: int
    n_relevant: int
    n_retrieved: int

    def as_dict(self) -> dict:
        return {
            f"precision@{self.k}": round(self.precision_at_k, 4),
            f"recall@{self.k}": round(self.recall_at_k, 4),
            "mrr": round(self.mrr, 4),
            f"ndcg@{self.k}": round(self.ndcg_at_k, 4),
            "map": round(self.average_precision, 4),
            "n_relevant": self.n_relevant,
            "n_retrieved": self.n_retrieved,
        }


def evaluate_ranking(
    ranked_ids: Sequence[int], relevant_ids: Iterable[int], k: int
) -> RankingMetrics:
    relevant = list(relevant_ids)
    return RankingMetrics(
        precision_at_k=precision_at_k(ranked_ids, relevant, k),
        recall_at_k=recall_at_k(ranked_ids, relevant, k),
        mrr=reciprocal_rank(ranked_ids, relevant),
        ndcg_at_k=ndcg_at_k(ranked_ids, relevant, k),
        average_precision=average_precision(ranked_ids, relevant),
        k=k,
        n_relevant=len(set(relevant)),
        n_retrieved=len(ranked_ids),
    )


def macro_average(metrics: Sequence[RankingMetrics]) -> dict:
    """Average per-query metrics, weighting every assignment equally.

    Macro rather than micro: an assignment with eight relevant notes should not
    count eight times as much as one with a single relevant note, because the
    student experiences both as one screen of results.
    """
    if not metrics:
        return {}
    n = len(metrics)
    k = metrics[0].k
    return {
        f"precision@{k}": round(sum(m.precision_at_k for m in metrics) / n, 4),
        f"recall@{k}": round(sum(m.recall_at_k for m in metrics) / n, 4),
        "mrr": round(sum(m.mrr for m in metrics) / n, 4),
        f"ndcg@{k}": round(sum(m.ndcg_at_k for m in metrics) / n, 4),
        "map": round(sum(m.average_precision for m in metrics) / n, 4),
        "queries": n,
    }
