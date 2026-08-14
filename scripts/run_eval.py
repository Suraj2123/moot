#!/usr/bin/env python3
"""Measure retrieval quality against the labelled set.

    python scripts/run_eval.py                 # score the current configuration
    python scripts/run_eval.py --sweep         # grid-search chunking and thresholds
    python scripts/run_eval.py --judge         # check the LLM judge against hand labels
    python scripts/run_eval.py --json out.json # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink import store  # noqa: E402
from studylink.config import load_settings  # noqa: E402
from studylink.evaluation.dataset import load_labels, resolve_labels  # noqa: E402
from studylink.evaluation.judge import RelevanceJudge, agreement  # noqa: E402
from studylink.evaluation.runner import format_report, format_sweep  # noqa: E402
from studylink.service import StudyLink  # noqa: E402


def run_judge(app: StudyLink) -> int:
    """Grade the labelled pairs with the LLM judge and compare with the hand labels."""
    pairs = load_labels(app.settings.labels_path)
    positives, negatives, unresolved = resolve_labels(app.conn, pairs)
    if unresolved:
        print("Warning: unresolved labels:")
        for item in unresolved:
            print(f"  - {item}")

    hand_labels: dict[tuple[int, int], bool] = {}
    to_judge = []
    for bucket, relevant in ((positives, True), (negatives, False)):
        for assignment_id, note_ids in bucket.items():
            assignment = store.get_assignment(app.conn, assignment_id)
            for note_id in note_ids:
                note = store.get_note(app.conn, note_id)
                if assignment and note:
                    hand_labels[(assignment_id, note_id)] = relevant
                    to_judge.append((assignment, note))

    if not to_judge:
        print("No labelled pairs to judge. Run scripts/seed_demo.py first.")
        return 1

    print(f"Judging {len(to_judge)} pair(s) with the LLM judge...")
    judgements = RelevanceJudge().judge_pairs(to_judge)
    result = agreement(judgements, hand_labels)

    print(f"\ncompared:       {result['compared']}")
    print(f"raw agreement:  {result['raw_agreement']:.3f}")
    print(f"Cohen's kappa:  {result['cohens_kappa']:.3f}")
    if result["cohens_kappa"] < 0.6:
        print("  -> Below ~0.6. Fix the judge prompt before using it to label anything new.")
    else:
        print("  -> Substantial agreement; the judge is usable for the unlabelled tail.")

    if result["disagreements"]:
        print("\ndisagreements (worth reading -- some will be your labels being wrong):")
        for item in result["disagreements"]:
            note = store.get_note(app.conn, item["note_id"])
            assignment = store.get_assignment(app.conn, item["assignment_id"])
            print(
                f"  {assignment.name[:40] if assignment else item['assignment_id']!r}"
                f" <- {note.title[:40] if note else item['note_id']!r}"
            )
            print(f"     human={item['human']} judge_grade={item['judge_grade']}: {item['judge_reason']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true", help="Grid-search retrieval settings")
    parser.add_argument("--judge", action="store_true", help="Validate the LLM judge against hand labels")
    parser.add_argument("--top-k", type=int, default=None, help="Override top_k")
    parser.add_argument("--json", type=Path, default=None, help="Write results as JSON")
    args = parser.parse_args()

    settings = load_settings()
    app = StudyLink(settings)

    if app.status().notes == 0:
        print("No notes in the database. Run: python scripts/seed_demo.py")
        return 1

    if args.judge:
        return run_judge(app)

    if args.top_k:
        app.set_retrieval_config(app.config.with_(top_k=args.top_k))

    if args.sweep:
        reports = app.sweep(
            chunk_sizes=(80, 120, 180, 300),
            chunk_overlaps=(0, 20, 40),
            top_k=args.top_k,
        )
        print(format_sweep(reports))
        print("\nBest configuration by recall:")
        print(format_report(reports[0]))
        if args.json:
            args.json.write_text(
                json.dumps([r.as_dict() for r in reports], indent=2) + "\n", encoding="utf-8"
            )
            print(f"\nWrote {args.json}")
        return 0

    report = app.evaluate()
    print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
