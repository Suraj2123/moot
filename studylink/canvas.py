"""Canvas LMS API client and sync.

Auth is a personal access token, read from the environment and sent as a bearer
token. It is never written to the database, logged, or echoed back to the UI.

Sync is on demand (a "Refresh" button), per the v1 scope -- no polling, no
webhooks. `sync_all` is idempotent: it upserts on `(course_id, canvas_id)` so
re-running it updates rows in place rather than duplicating them.
"""

from __future__ import annotations

from sqlalchemy import Connection

import html
import re
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from . import store

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def strip_html(raw: str) -> str:
    """Canvas assignment descriptions are HTML; embeddings want plain text.

    Block-level tags become newlines so paragraph structure survives, which the
    chunker then uses as split points.
    """
    if not raw:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    # Block elements become blank lines so the chunker can see paragraph
    # boundaries; list items and table rows are single line breaks.
    text = re.sub(r"(?i)</(p|div|h[1-6])>", "\n\n", text)
    text = re.sub(r"(?i)</(li|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


class CanvasError(RuntimeError):
    pass


@dataclass
class SyncResult:
    courses: int
    assignments: int
    errors: list[str]

    @property
    def summary(self) -> str:
        base = f"{self.courses} course(s), {self.assignments} assignment(s)"
        if self.errors:
            base += f" -- {len(self.errors)} warning(s)"
        return base


class CanvasClient:
    def __init__(self, api_url: str, api_token: str, timeout: int = 30) -> None:
        if not api_url or not api_token:
            raise CanvasError(
                "Canvas is not configured. Set CANVAS_API_URL and CANVAS_API_TOKEN."
            )
        self.base_url = api_url.rstrip("/")
        self._token = api_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _paginate(self, path: str, params: Optional[dict] = None) -> Iterator[dict[str, Any]]:
        """Follow Canvas's RFC 5988 `Link: <...>; rel="next"` pagination."""
        import requests

        url = f"{self.base_url}/api/v1{path}"
        query = dict(params or {})
        query.setdefault("per_page", 100)

        while url:
            response = requests.get(url, headers=self._headers(), params=query, timeout=self.timeout)
            if response.status_code == 401:
                raise CanvasError("Canvas rejected the API token (401).")
            if response.status_code == 403:
                raise CanvasError("Canvas denied access (403) -- check the token's scopes.")
            response.raise_for_status()

            payload = response.json()
            if isinstance(payload, dict):  # error envelopes come back as objects
                raise CanvasError(f"Unexpected Canvas response for {path}: {payload}")
            yield from payload

            url = response.links.get("next", {}).get("url")
            query = {}  # the `next` URL already carries the query string

    def list_courses(self) -> list[dict[str, Any]]:
        return list(self._paginate("/courses", {"enrollment_state": "active"}))

    def list_assignments(self, course_id: str | int) -> list[dict[str, Any]]:
        return list(self._paginate(f"/courses/{course_id}/assignments"))

    def list_modules(self, course_id: str | int) -> list[dict[str, Any]]:
        """Module structure -- used to group notes by course topic in the UI."""
        return list(self._paginate(f"/courses/{course_id}/modules", {"include[]": "items"}))

    def whoami(self) -> dict[str, Any]:
        import requests

        response = requests.get(
            f"{self.base_url}/api/v1/users/self",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def sync_all(conn: Connection, client: CanvasClient, user_id: int) -> SyncResult:
    """Pull every active course and its assignments into the local database."""
    errors: list[str] = []
    course_count = 0
    assignment_count = 0

    for course in client.list_courses():
        name = course.get("name") or f"Course {course.get('id')}"
        try:
            course_id = store.upsert_course(
                conn,
                canvas_id=str(course["id"]),
                name=name,
                course_code=course.get("course_code") or "",
                user_id=user_id,
            )
        except Exception as exc:  # a malformed course should not abort the whole sync
            errors.append(f"course {course.get('id')}: {exc}")
            continue
        course_count += 1

        try:
            assignments = client.list_assignments(course["id"])
        except Exception as exc:
            errors.append(f"assignments for {name}: {exc}")
            continue

        for assignment in assignments:
            try:
                store.upsert_assignment(
                    conn,
                    course_id=course_id,
                    canvas_id=str(assignment["id"]),
                    name=assignment.get("name") or "Untitled assignment",
                    description=strip_html(assignment.get("description") or ""),
                    due_at=assignment.get("due_at"),
                    points_possible=assignment.get("points_possible"),
                    submission_types=",".join(assignment.get("submission_types") or []),
                    html_url=assignment.get("html_url"),
                    user_id=user_id,
                )
                assignment_count += 1
            except Exception as exc:
                errors.append(f"assignment {assignment.get('id')}: {exc}")

    store.record_sync(
        conn, course_count, assignment_count, "; ".join(errors[:5]), user_id=user_id
    )
    return SyncResult(courses=course_count, assignments=assignment_count, errors=errors)
