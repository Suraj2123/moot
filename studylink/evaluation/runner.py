"""Run the retriever against the labelled set and sweep configurations.

The sweep is the point of the whole eval module: it turns "the matching seems to
work" into "chunk=120/30 beats chunk=300/60 by 0.14 recall@5 on 18 labelled
pairs", which is a claim you can act on and defend.
"""

from __future__ import annotations

from sqlalchemy import Connection

from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .. import store
from ..config import RetrievalConfig
from ..embeddings import EmbeddingProvider
from ..indexing import Indexer
from ..models import Assignment
from ..retrieval import Retriever
from .dataset import load_labels, resolve_labels
from .metrics import RankingMetrics, evaluate_ranking, macro_average


@dataclass
class AssignmentEval:
    assignment: Assignment
    ranked_note_ids: list[int]
    relevant_note_ids: list[int]
    metrics: RankingMetrics
    false_positives: list[int] = field(default_factory=list)
    scores: dict[int, float] = field(default_factory=dict)


@dataclass
class EvalReport:
    config: RetrievalConfig
    provider: str
    per_assignment: list[AssignmentEval]
    macro: dict
    judged_only_macro: dict
    unresolved_labels: list[str] = field(default_factory=list)
    n_positive_labels: int = 0
    n_negative_labels: int = 0

    @property
    def headline(self) -> str:
        k = self.config.top_k
        if not self.macro:
            return "no labelled assignments to evaluate"
        return (
            f"recall@{k}={self.macro[f'recall@{k}']:.3f}  "
            f"precision@{k}={self.macro[f'precision@{k}']:.3f}  "
            f"mrr={self.macro['mrr']:.3f}  "
            f"ndcg@{k}={self.macro[f'ndcg@{k}']:.3f}"
        )

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "config": {
                "chunk_size": self.config.chunk_size,
                "chunk_overlap": self.config.chunk_overlap,
                "top_k": self.config.top_k,
                "score_threshold": self.config.score_threshold,
            },
            "macro": self.macro,
            "judged_only_macro": self.judged_only_macro,
            "labels": {
                "positive": self.n_positive_labels,
                "negative": self.n_negative_labels,
                "unresolved": self.unresolved_labels,
            },
        }


def evaluate_config(
    conn: Connection,
    provider: EmbeddingProvider,
    config: RetrievalConfig,
    labels_path: Path,
    user_id: int,
    reindex: bool = True,
) -> EvalReport:
    """Index under `config`, retrieve for every labelled assignment, score the rankings."""
    if reindex:
        Indexer(conn, provider, config, user_id).reindex()

    pairs = load_labels(labels_path)
    positives, negatives, unresolved = resolve_labels(conn, pairs, user_id)
    retriever = Retriever(conn, provider, config, user_id)

    # Rank deeper than k so MRR and MAP can see relevant notes that fall just
    # outside the displayed window; @k metrics still use the top k.
    depth = max(config.top_k * 3, 15)

    per_assignment: list[AssignmentEval] = []
    judged_metrics: list[RankingMetrics] = []

    for assignment_id, relevant in sorted(positives.items()):
        assignment = store.get_assignment(conn, assignment_id, user_id)
        if assignment is None:
            continue

        matches = retriever.notes_for_assignment(
            assignment, top_k=depth, apply_threshold=False
        )
        ranked = [m.note.id for m in matches]
        scores = {m.note.id: m.score for m in matches}

        metrics = evaluate_ranking(ranked, relevant, k=config.top_k)
        negatives_here = negatives.get(assignment_id, set())
        false_positives = [nid for nid in ranked[: config.top_k] if nid in negatives_here]

        per_assignment.append(
            AssignmentEval(
                assignment=assignment,
                ranked_note_ids=ranked,
                relevant_note_ids=sorted(relevant),
                metrics=metrics,
                false_positives=false_positives,
                scores=scores,
            )
        )

        # "Judged only" restricts the ranking to notes that were actually labelled
        # either way. The full metric treats every unlabelled note as irrelevant,
        # which understates precision when the label set is small and incomplete;
        # the judged-only metric overstates it by ignoring unknowns. The truth is
        # between them, and seeing both keeps you honest about which you quoted.
        judged = relevant | negatives_here
        judged_ranked = [nid for nid in ranked if nid in judged]
        judged_metrics.append(evaluate_ranking(judged_ranked, relevant, k=config.top_k))

    return EvalReport(
        config=config,
        provider=provider.name,
        per_assignment=per_assignment,
        macro=macro_average([e.metrics for e in per_assignment]),
        judged_only_macro=macro_average(judged_metrics),
        unresolved_labels=unresolved,
        n_positive_labels=sum(len(v) for v in positives.values()),
        n_negative_labels=sum(len(v) for v in negatives.values()),
    )


def sweep(
    conn: Connection,
    provider: EmbeddingProvider,
    labels_path: Path,
    user_id: int,
    base: Optional[RetrievalConfig] = None,
    chunk_sizes: Sequence[int] = (120, 180, 300),
    chunk_overlaps: Sequence[int] = (20, 40),
    thresholds: Sequence[float] = (0.15,),
    top_k: Optional[int] = None,
) -> list[EvalReport]:
    """Grid-search chunking and threshold settings, best recall first.

    Reindexes for every chunk-size/overlap combination, which is the expensive
    part; thresholds are swept without reindexing since they only filter results.
    """
    base = base or RetrievalConfig()
    reports: list[EvalReport] = []

    for size, overlap in product(chunk_sizes, chunk_overlaps):
        if overlap >= size:
            continue
        for threshold in thresholds:
            config = base.with_(
                chunk_size=size,
                chunk_overlap=overlap,
                score_threshold=threshold,
                top_k=top_k or base.top_k,
            )
            reports.append(evaluate_config(conn, provider, config, labels_path, user_id))

    k = (top_k or base.top_k)
    reports.sort(key=lambda r: r.macro.get(f"recall@{k}", 0.0), reverse=True)
    return reports


def format_report(report: EvalReport) -> str:
    lines = [
        f"provider: {report.provider}",
        f"config:   {report.config.label}",
        f"labels:   {report.n_positive_labels} positive, {report.n_negative_labels} negative",
        "",
        "macro (unlabelled notes counted as irrelevant):",
        f"  {report.macro}",
        "macro (judged pairs only):",
        f"  {report.judged_only_macro}",
    ]
    if report.unresolved_labels:
        lines += ["", "UNRESOLVED LABELS (fix these -- they are silently shrinking the eval set):"]
        lines += [f"  - {u}" for u in report.unresolved_labels]

    lines += ["", "per assignment:"]
    for entry in report.per_assignment:
        m = entry.metrics.as_dict()
        k = entry.metrics.k
        lines.append(
            f"  {entry.assignment.name[:52]:<52} "
            f"R@{k}={m[f'recall@{k}']:.2f} P@{k}={m[f'precision@{k}']:.2f} MRR={m['mrr']:.2f}"
        )
        missed = [nid for nid in entry.relevant_note_ids if nid not in entry.ranked_note_ids[:k]]
        if missed:
            lines.append(f"      missed note ids: {missed}")
        if entry.false_positives:
            lines.append(f"      labelled-irrelevant notes in top {k}: {entry.false_positives}")
    return "\n".join(lines)


def format_sweep(reports: Iterable[EvalReport]) -> str:
    rows = ["config".ljust(38) + "recall@k  prec@k   mrr     ndcg@k"]
    for report in reports:
        k = report.config.top_k
        m = report.macro
        if not m:
            continue
        rows.append(
            f"{report.config.label:<38}"
            f"{m[f'recall@{k}']:<10.3f}{m[f'precision@{k}']:<9.3f}"
            f"{m['mrr']:<8.3f}{m[f'ndcg@{k}']:.3f}"
        )
    return "\n".join(rows)
