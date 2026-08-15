"""The labelled eval set.

Labels live in JSON (`data/eval/labels.json`) rather than only in SQLite, for one
reason: they are the most valuable artefact in the project and the slowest to
recreate. In a file they are diffable, reviewable, and survive `rm data/studylink.db`.
SQLite holds a mirror so the UI can label interactively.

Pairs are identified by *title* rather than row id, so the labels stay valid when
the database is rebuilt from scratch and every id changes.
"""

from __future__ import annotations

from sqlalchemy import Connection

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import store


@dataclass
class LabeledPair:
    assignment_name: str
    note_title: str
    relevant: bool
    rationale: str = ""

    def key(self) -> tuple[str, str]:
        return (self.assignment_name.strip().lower(), self.note_title.strip().lower())


def save_labels(pairs: list[LabeledPair], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": (
            "Hand-labelled note<->assignment pairs used to measure retrieval quality. "
            "relevant=true means the note SHOULD be retrieved for that assignment; "
            "relevant=false means it is a plausible-looking distractor that should NOT be."
        ),
        "pairs": [asdict(p) for p in pairs],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_labels(path: Path) -> list[LabeledPair]:
    if not Path(path).exists():
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        LabeledPair(
            assignment_name=p["assignment_name"],
            note_title=p["note_title"],
            relevant=bool(p["relevant"]),
            rationale=p.get("rationale", ""),
        )
        for p in payload.get("pairs", [])
    ]


def resolve_labels(
    conn: Connection, pairs: list[LabeledPair], user_id: int
) -> tuple[dict[int, set[int]], dict[int, set[int]], list[str]]:
    """Map title-keyed labels onto database ids.

    Returns `(positives, negatives, unresolved)` where the dicts are
    assignment_id -> set(note_id). Unresolved labels are reported rather than
    dropped silently: a typo in a label file that quietly shrinks the eval set is
    how you end up trusting a metric computed over three pairs.
    """
    assignments = {a.name.strip().lower(): a.id for a in store.list_assignments(conn, user_id)}
    notes = {n.title.strip().lower(): n.id for n in store.list_notes(conn, user_id)}

    positives: dict[int, set[int]] = {}
    negatives: dict[int, set[int]] = {}
    unresolved: list[str] = []

    for pair in pairs:
        assignment_id = assignments.get(pair.assignment_name.strip().lower())
        note_id = notes.get(pair.note_title.strip().lower())
        if assignment_id is None or note_id is None:
            missing = "assignment" if assignment_id is None else "note"
            unresolved.append(f"{missing} not found for label ({pair.assignment_name!r}, {pair.note_title!r})")
            continue
        bucket = positives if pair.relevant else negatives
        bucket.setdefault(assignment_id, set()).add(note_id)

    return positives, negatives, unresolved


def sync_to_db(conn: Connection, pairs: list[LabeledPair], user_id: int) -> int:
    """Mirror file labels into SQLite so the UI can show and edit them."""
    positives, negatives, _ = resolve_labels(conn, pairs, user_id)
    written = 0
    for assignment_id, note_ids in positives.items():
        for note_id in note_ids:
            store.set_label(conn, assignment_id, note_id, True, user_id=user_id)
            written += 1
    for assignment_id, note_ids in negatives.items():
        for note_id in note_ids:
            store.set_label(conn, assignment_id, note_id, False, user_id=user_id)
            written += 1
    return written


def export_from_db(conn: Connection, user_id: int) -> list[LabeledPair]:
    """Pull interactively-created labels back out into file form."""
    return [
        LabeledPair(
            assignment_name=row["assignment_name"],
            note_title=row["note_title"],
            relevant=bool(row["relevant"]),
            rationale=row.get("rationale", "") or "",
        )
        for row in store.list_labels(conn, user_id)
    ]
