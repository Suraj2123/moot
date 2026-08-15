#!/usr/bin/env python3
"""Export labels created in the UI back to `data/eval/labels.json`.

Labels made interactively live in SQLite. This writes them out to the JSON file
so they end up in version control, where they belong -- the label set is the
slowest thing in the project to recreate.

    python scripts/export_labels.py [--out data/eval/labels.json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink.config import load_settings  # noqa: E402
from studylink.evaluation.dataset import export_from_db, save_labels  # noqa: E402
from studylink.service import StudyLink  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings()
    app = StudyLink(settings)
    pairs = export_from_db(app.conn, app.user_id)
    if not pairs:
        print("No labels in the database.")
        return 1

    out = args.out or settings.labels_path
    save_labels(pairs, out)
    positive = sum(1 for p in pairs if p.relevant)
    print(f"Wrote {len(pairs)} label(s) ({positive} positive) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
