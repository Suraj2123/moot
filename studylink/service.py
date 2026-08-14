"""The application facade.

One object that owns the connection, the embedding provider, the index, the
retriever, and the agent. The Streamlit UI, the FastAPI app, and the scripts all
drive this rather than wiring the pieces up themselves, so there is exactly one
place where "how StudyLink is assembled" is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import store
from .agent import WorkSessionAgent
from .canvas import CanvasClient, SyncResult, sync_all
from .config import RetrievalConfig, Settings, load_settings
from .db import connect
from .embeddings import build_provider
from .evaluation.dataset import load_labels, sync_to_db
from .evaluation.runner import EvalReport, evaluate_config, sweep
from .indexing import Indexer, IndexStats
from .models import Assignment, AssignmentMatch, Note, NoteMatch


@dataclass
class Status:
    courses: int
    assignments: int
    notes: int
    chunks: int
    chunk_vectors: int
    assignment_vectors: int
    provider: str
    last_sync: Optional[dict]
    index_stale: bool

    @property
    def ready_for_matching(self) -> bool:
        return self.chunk_vectors > 0 and self.assignment_vectors > 0


class StudyLink:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or load_settings()
        self.conn = connect(self.settings.db_path)
        self.provider = build_provider(
            self.settings.embedding_provider,
            self.settings.embedding_model,
            self.settings.voyage_api_key,
        )
        self.config = self.settings.retrieval
        self.indexer = Indexer(self.conn, self.provider, self.config)
        self.retriever = self.indexer_retriever()

    def indexer_retriever(self):
        from .retrieval import Retriever

        return Retriever(self.conn, self.provider, self.config)

    # ------------------------------------------------------------------- config

    def set_retrieval_config(self, config: RetrievalConfig) -> None:
        """Swap retrieval settings at runtime (used by the UI sliders and the sweep)."""
        self.config = config
        self.indexer = Indexer(self.conn, self.provider, config)
        self.retriever = self.indexer_retriever()

    # -------------------------------------------------------------------- canvas

    def sync_canvas(self) -> SyncResult:
        client = CanvasClient(self.settings.canvas_api_url, self.settings.canvas_api_token)
        result = sync_all(self.conn, client)
        self.indexer.embed_assignments()
        return result

    # --------------------------------------------------------------------- notes

    def add_note(
        self,
        title: str,
        body: str,
        course_id: Optional[int] = None,
        source_type: str = "note",
        reindex: bool = True,
    ) -> int:
        note_id = store.create_note(self.conn, title, body, course_id, source_type)
        if reindex:
            self.reindex()
        return note_id

    def delete_note(self, note_id: int) -> None:
        store.delete_note(self.conn, note_id)

    def list_notes(self, course_id: Optional[int] = None, search: str = "") -> list[Note]:
        return store.list_notes(self.conn, course_id=course_id, search=search)

    def list_courses(self):
        return store.list_courses(self.conn)

    def list_assignments(self, course_id: Optional[int] = None) -> list[Assignment]:
        return store.list_assignments(self.conn, course_id)

    def get_assignment(self, assignment_id: int) -> Optional[Assignment]:
        return store.get_assignment(self.conn, assignment_id)

    def get_note(self, note_id: int) -> Optional[Note]:
        return store.get_note(self.conn, note_id)

    # ------------------------------------------------------------------ indexing

    def reindex(self, force: bool = False) -> IndexStats:
        return self.indexer.reindex(force=force)

    # ----------------------------------------------------------------- retrieval

    def matches_for_assignment(
        self, assignment_id: int, top_k: Optional[int] = None
    ) -> list[NoteMatch]:
        assignment = store.get_assignment(self.conn, assignment_id)
        if assignment is None:
            return []
        return self.retriever.notes_for_assignment(assignment, top_k=top_k)

    def assignments_for_note(self, note_id: int, top_k: int = 5) -> list[AssignmentMatch]:
        note = store.get_note(self.conn, note_id)
        if note is None:
            return []
        return self.retriever.assignments_for_note(note, top_k=top_k)

    def search_notes(self, query: str, top_k: int = 5) -> list[NoteMatch]:
        return self.retriever.search_notes(query, top_k=top_k)

    # --------------------------------------------------------------------- agent

    def agent(self) -> WorkSessionAgent:
        return WorkSessionAgent(self.conn, self.retriever)

    # ---------------------------------------------------------------- evaluation

    def load_eval_labels(self, path: Optional[Path] = None) -> int:
        pairs = load_labels(path or self.settings.labels_path)
        return sync_to_db(self.conn, pairs)

    def evaluate(self, config: Optional[RetrievalConfig] = None) -> EvalReport:
        return evaluate_config(
            self.conn,
            self.provider,
            config or self.config,
            self.settings.labels_path,
        )

    def sweep(self, **kwargs) -> list[EvalReport]:
        reports = sweep(self.conn, self.provider, self.settings.labels_path, base=self.config, **kwargs)
        # The sweep leaves the index chunked under whichever config ran last;
        # restore the active configuration so the app is not left inconsistent.
        self.reindex()
        return reports

    # -------------------------------------------------------------------- status

    def status(self) -> Status:
        counts = {
            table: int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in ("courses", "assignments", "notes", "chunks")
        }
        chunk_vectors = self.indexer.vectors.count("chunk", self.provider.name)
        assignment_vectors = self.indexer.vectors.count("assignment", self.provider.name)

        params = store.chunking_params_in_use(self.conn)
        index_stale = (
            counts["chunks"] > chunk_vectors
            or counts["assignments"] > assignment_vectors
            or (params is not None and params != (self.config.chunk_size, self.config.chunk_overlap))
        )

        return Status(
            courses=counts["courses"],
            assignments=counts["assignments"],
            notes=counts["notes"],
            chunks=counts["chunks"],
            chunk_vectors=chunk_vectors,
            assignment_vectors=assignment_vectors,
            provider=self.provider.name,
            last_sync=store.last_sync(self.conn),
            index_stale=index_stale,
        )
