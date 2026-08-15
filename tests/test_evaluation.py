from __future__ import annotations

from studylink.evaluation.judge import cohens_kappa
from studylink.evaluation.metrics import (
    average_precision,
    evaluate_ranking,
    macro_average,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_precision_and_recall_at_k():
    ranked = [1, 2, 3, 4, 5]
    relevant = {1, 3, 9}

    assert precision_at_k(ranked, relevant, 5) == 2 / 5
    assert recall_at_k(ranked, relevant, 5) == 2 / 3
    # Deepening k cannot lower recall.
    assert recall_at_k(ranked, relevant, 3) <= recall_at_k(ranked, relevant, 5)


def test_precision_is_capped_when_relevant_set_is_small():
    # Documents the caveat the eval output warns about: with one relevant note,
    # a perfect retriever still scores 0.2 at k=5.
    assert precision_at_k([1, 2, 3, 4, 5], {1}, 5) == 0.2


def test_reciprocal_rank_rewards_early_hits():
    assert reciprocal_rank([9, 1], {1}) == 0.5
    assert reciprocal_rank([1, 9], {1}) == 1.0
    assert reciprocal_rank([7, 8], {1}) == 0.0


def test_ndcg_prefers_better_ordering():
    good = ndcg_at_k([1, 2, 9, 8], {1, 2}, 4)
    bad = ndcg_at_k([9, 8, 1, 2], {1, 2}, 4)
    assert good == 1.0
    assert good > bad


def test_average_precision():
    assert average_precision([1, 2], {1, 2}) == 1.0
    assert average_precision([9, 1, 8, 2], {1, 2}) < 1.0
    assert average_precision([9, 8], {1}) == 0.0


def test_metrics_handle_an_empty_relevant_set():
    metrics = evaluate_ranking([1, 2, 3], [], k=3)
    assert metrics.recall_at_k == 0.0
    assert metrics.average_precision == 0.0


def test_macro_average_weights_queries_equally():
    a = evaluate_ranking([1], {1}, k=1)                 # perfect, 1 relevant
    b = evaluate_ranking([9], {1, 2, 3, 4}, k=1)        # miss, 4 relevant
    averaged = macro_average([a, b])
    assert averaged["queries"] == 2
    assert averaged["recall@1"] == 0.5  # not weighted by relevant-set size


def test_cohens_kappa_corrects_for_chance():
    perfect = cohens_kappa([True, False, True, False], [True, False, True, False])
    assert perfect == 1.0

    # Both annotators say "irrelevant" almost always: high raw agreement,
    # near-zero kappa.
    a = [False] * 9 + [True]
    b = [False] * 10
    assert cohens_kappa(a, b) <= 0.0


def test_cohens_kappa_handles_degenerate_input():
    assert cohens_kappa([], []) == 0.0
    assert cohens_kappa([True], [True, False]) == 0.0


def test_evaluate_config_end_to_end(corpus, tmp_path):
    from studylink.config import RetrievalConfig
    from studylink.embeddings import build_provider
    from studylink.evaluation.dataset import LabeledPair, save_labels
    from studylink.evaluation.runner import evaluate_config

    labels_path = tmp_path / "labels.json"
    save_labels(
        [
            LabeledPair("Problem Set: Gradient Descent", "Lecture on gradient descent", True),
            LabeledPair("Problem Set: Gradient Descent", "Lecture on the alliance system", False),
            LabeledPair("Essay: Causes of the First World War", "Lecture on the alliance system", True),
            LabeledPair("Essay: Causes of the First World War", "Lecture on gradient descent", False),
        ],
        labels_path,
    )

    report = evaluate_config(
        corpus["conn"],
        build_provider("hash", "hash-256"),
        RetrievalConfig(chunk_size=60, chunk_overlap=10, top_k=2, score_threshold=0.05),
        labels_path,
        corpus["user_id"],
    )

    assert report.n_positive_labels == 2
    assert report.n_negative_labels == 2
    assert report.unresolved_labels == []
    assert report.macro["recall@2"] == 1.0


def test_unresolvable_labels_are_reported_not_dropped(corpus, tmp_path):
    from studylink.config import RetrievalConfig
    from studylink.embeddings import build_provider
    from studylink.evaluation.dataset import LabeledPair, save_labels
    from studylink.evaluation.runner import evaluate_config

    labels_path = tmp_path / "labels.json"
    save_labels(
        [
            LabeledPair("Problem Set: Gradient Descent", "Lecture on gradient descent", True),
            LabeledPair("Assignment That Does Not Exist", "Lecture on gradient descent", True),
        ],
        labels_path,
    )

    report = evaluate_config(
        corpus["conn"],
        build_provider("hash", "hash-256"),
        RetrievalConfig(chunk_size=60, chunk_overlap=10, top_k=2, score_threshold=0.05),
        labels_path,
        corpus["user_id"],
    )
    assert len(report.unresolved_labels) == 1
    assert "Assignment That Does Not Exist" in report.unresolved_labels[0]
