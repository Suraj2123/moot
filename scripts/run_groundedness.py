#!/usr/bin/env python3
"""Score the chat layer against data/eval/groundedness.json.

    python scripts/run_groundedness.py            # needs ANTHROPIC_API_KEY
    python scripts/run_groundedness.py --dry-run  # no key, no calls

The retrieval eval asks whether the right notes came back. This asks the
question on top of it: given those notes, did the answer stay inside them.
They fail independently.

Read `refusal accuracy` first. Citation validity and coverage can both look
excellent on a system that confidently answers questions the corpus cannot
support -- refusal accuracy is the number that catches it, and answer rate is
the counterweight that stops "refuse everything" scoring well.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink.chat import NoteChat  # noqa: E402
from studylink.evaluation.groundedness import (  # noqa: E402
    evaluate,
    format_report,
    load_cases,
)
from studylink.service import StudyLink  # noqa: E402

DEFAULT_CASES = Path("data/eval/groundedness.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be asked and what retrieval finds, without calling the model",
    )
    args = parser.parse_args()

    app = StudyLink()
    cases = load_cases(args.cases)
    chat = NoteChat(app.conn, app.retriever, user_id=app.user_id)

    if args.dry_run:
        print(f"{len(cases)} case(s) against {app.status().notes} note(s)\n")
        print(f"{'question':<54}{'expected':<14}retrieved")
        for case in cases:
            matches = chat.retrieve(case.question)
            expected = "answerable" if case.answerable else "unanswerable"
            print(f"  {case.question[:52]:<52}{expected:<14}{len(matches)}")
        print(
            "\nA case marked unanswerable that retrieves nothing will refuse "
            "without a model call."
        )
        return 0

    print(format_report(evaluate(chat, cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
