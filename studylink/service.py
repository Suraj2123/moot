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

from sqlalchemy import Connection, func, select

from . import cards as cards_module
from . import outline as outline_module
from . import progress as progress_module
from . import credentials, schema, store
from . import usage as usage_module
from .agent import WorkSessionAgent
from .canvas import CanvasClient, CanvasError, SyncResult, sync_all
from .config import RetrievalConfig, Settings, load_settings
from .context import UserContext
from .db import connect
from .embeddings import build_provider
from .errors import NotFoundError, assert_owned
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
    def __init__(
        self,
        settings: Optional[Settings] = None,
        user: Optional[UserContext] = None,
        conn: Optional[Connection] = None,
    ) -> None:
        self.settings = settings or load_settings()
        # An explicit connection is how the API scopes a service object to a
        # single request. Without it, `user` would be mutable state on an object
        # shared across concurrent requests, and two callers would race over
        # whose identity is set -- which is an authorisation bug, not a
        # performance one. The CLI and the tests pass nothing and get their own.
        #
        # sqlalchemy_url honours DATABASE_URL and falls back to SQLite at db_path.
        self.conn = conn if conn is not None else connect(self.settings.sqlalchemy_url)
        self.provider = build_provider(
            self.settings.embedding_provider,
            self.settings.embedding_model,
            self.settings.voyage_api_key,
        )
        self.config = self.settings.retrieval
        # Callers that already know who they are pass a context; the CLI, the
        # seeder, and local dev fall back to the single local user.
        self.user = user or UserContext.local(self.conn)

    @property
    def user(self) -> UserContext:
        return self._user

    @user.setter
    def user(self, context: UserContext) -> None:
        """Rebuild the index and retriever whenever the acting user changes.

        These hold a user id captured at construction. Leaving them stale after
        a user switch is the kind of bug that hides on SQLite -- where a reset
        database reuses rowid 1, so the stale id happens to still match -- and
        only surfaces on Postgres, whose sequences keep counting.
        """
        self._user = context
        self.indexer = Indexer(self.conn, self.provider, self.config, context.user_id)
        self.retriever = self.indexer_retriever()

    @property
    def user_id(self) -> int:
        """Shorthand for the owning user's id, read-only by design."""
        return self.user.user_id

    def indexer_retriever(self):
        from .retrieval import Retriever

        return Retriever(self.conn, self.provider, self.config, self.user_id)

    # ------------------------------------------------------------------- config

    def set_retrieval_config(self, config: RetrievalConfig) -> None:
        """Swap retrieval settings at runtime (used by the UI sliders and the sweep)."""
        self.config = config
        self.indexer = Indexer(self.conn, self.provider, config, self.user_id)
        self.retriever = self.indexer_retriever()

    # -------------------------------------------------------------------- canvas

    def canvas_connection(self):
        """This user's Canvas connection status, or None. Never a token."""
        return credentials.get_connection(self.conn, self.user_id)

    def sync_canvas(self) -> SyncResult:
        """Sync using *this user's* stored credentials.

        Falls back to the environment only when nothing is stored and the
        acting user is the local one. That keeps the CLI, the seeder, and
        single-user local development working exactly as before, while making it
        impossible for a web request to pick up somebody else's token from the
        process environment.
        """
        stored = credentials.get_token(self.conn, self.user_id)
        if stored is not None:
            api_url, api_token = stored
        elif self.user.auth_source == "local" and self.settings.canvas_configured:
            api_url, api_token = self.settings.canvas_api_url, self.settings.canvas_api_token
        else:
            raise CanvasError(
                "Canvas is not connected for this account. Connect it at "
                "POST /canvas/connect."
            )

        client = CanvasClient(api_url, api_token)
        result = sync_all(self.conn, client, self.user_id)
        self.indexer.embed_assignments()

        if stored is not None:
            credentials.mark_verified(self.conn, self.user_id)
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
        note_id = store.create_note(self.conn, title, body, course_id, source_type, self.user_id)
        self.sync_note_cards(note_id, title, body)
        if reindex:
            self.reindex()
        return note_id

    def update_note(
        self,
        note_id: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        course_id: Optional[int] = None,
        clear_course: bool = False,
    ) -> bool:
        """Edit a note. Returns whether the text changed and needs reindexing.

        Only a body change matters to retrieval. Renaming a note or filing it
        under a course leaves every chunk still accurate, so those do not
        invalidate an index that may have taken a while to build.

        A body change does invalidate it, and the invalidation has to be
        explicit: the indexer decides what to rebuild by comparing chunking
        parameters, which are unchanged by an edit, so without dropping the
        chunks here the note would keep matching on text the student removed.
        """
        assert_owned(self.conn, "notes", note_id, self.user_id)
        current = store.get_note(self.conn, note_id, self.user_id)
        if current is None:  # pragma: no cover - assert_owned already raised
            raise LookupError(note_id)

        new_title = current.title if title is None else title.strip()
        new_body = current.body if body is None else body
        body_changed = new_body != current.body

        store.update_note(self.conn, note_id, new_title, new_body, self.user_id)
        if clear_course or course_id is not None:
            store.set_note_course(
                self.conn, note_id, None if clear_course else course_id, self.user_id
            )
        if body_changed:
            store.clear_chunks(self.conn, note_id, self.user_id)
        # Always, not only on a body change: renaming a note renames the deck
        # it owns, and leaving that stale is a deck the student cannot find.
        self.sync_note_cards(note_id, new_title, new_body)
        return body_changed

    def delete_note(self, note_id: int) -> None:
        assert_owned(self.conn, "notes", note_id, self.user_id)
        store.delete_note(self.conn, note_id, self.user_id)

    def note_index_status(self) -> dict[int, str]:
        """note id -> "indexed" | "stale", for the whole account in one query."""
        return store.note_index_status(
            self.conn,
            self.user_id,
            model=self.settings.embedding_model,
            chunk_size=self.settings.retrieval.chunk_size,
            chunk_overlap=self.settings.retrieval.chunk_overlap,
        )

    def list_notes(self, course_id: Optional[int] = None, search: str = "") -> list[Note]:
        return store.list_notes(self.conn, self.user_id, course_id=course_id, search=search)

    def list_courses(self):
        return store.list_courses(self.conn, self.user_id)

    def list_assignments(self, course_id: Optional[int] = None) -> list[Assignment]:
        return store.list_assignments(self.conn, self.user_id, course_id)

    def get_assignment(self, assignment_id: int) -> Optional[Assignment]:
        return store.get_assignment(self.conn, assignment_id, self.user_id)

    def get_note(self, note_id: int) -> Optional[Note]:
        return store.get_note(self.conn, note_id, self.user_id)

    # ------------------------------------------------------------------ indexing

    def reindex(self, force: bool = False) -> IndexStats:
        return self.indexer.reindex(force=force)

    # ----------------------------------------------------------------- retrieval

    def matches_for_assignment(
        self, assignment_id: int, top_k: Optional[int] = None
    ) -> list[NoteMatch]:
        assert_owned(self.conn, "assignments", assignment_id, self.user_id)
        assignment = store.get_assignment(self.conn, assignment_id, self.user_id)
        if assignment is None:  # pragma: no cover - assert_owned already raised
            raise NotFoundError("assignments", assignment_id)
        return self.retriever.notes_for_assignment(assignment, top_k=top_k)

    def assignments_for_note(self, note_id: int, top_k: int = 5) -> list[AssignmentMatch]:
        assert_owned(self.conn, "notes", note_id, self.user_id)
        note = store.get_note(self.conn, note_id, self.user_id)
        if note is None:  # pragma: no cover - assert_owned already raised
            raise NotFoundError("notes", note_id)
        return self.retriever.assignments_for_note(note, top_k=top_k)

    def search_notes(self, query: str, top_k: int = 5) -> list[NoteMatch]:
        return self.retriever.search_notes(query, top_k=top_k)

    # --------------------------------------------------------------------- agent

    def agent(self) -> WorkSessionAgent:
        return WorkSessionAgent(self.conn, self.retriever)

    # --------------------------------------------------------------------- cards

    def make_deck_from_note(
        self, note_id: int, count: int = 10, title: Optional[str] = None, writer=None
    ) -> dict:
        """Generate a deck of flashcards from one note.

        Ungrounded cards are dropped before anything is stored, and the count
        is reported. A card that teaches something the note never said is worse
        than no card at all -- the student will study it and believe it -- so
        the failure mode here is "fewer cards than asked for", never "a card
        that cannot be traced".
        """
        assert_owned(self.conn, "notes", note_id, self.user_id)
        note = store.get_note(self.conn, note_id, self.user_id)
        if note is None:  # pragma: no cover - assert_owned already raised
            raise NotFoundError("notes", note_id)

        usage_module.check_budget(self.conn, self.user_id)
        writer = writer or cards_module.CardWriter()
        generated = writer.write(note.title, note.body, count=count)

        if generated.usage:
            usage_module.record(
                self.conn, self.user_id, "cards", writer.model,
                int(generated.usage.get("input_tokens", 0)),
                int(generated.usage.get("output_tokens", 0)),
            )

        if not generated.cards:
            raise cards_module.CardError(
                "No cards could be grounded in that note. Every card has to quote "
                "the note it came from, and none of the generated ones did."
            )

        deck_id = store.create_deck(
            self.conn, self.user_id,
            title=(title or "").strip() or note.title,
            course_id=note.course_id,
        )
        for draft in generated.cards:
            draft.note_id = note_id
        card_ids = store.add_cards(self.conn, self.user_id, deck_id, generated.cards)

        return {
            "deck_id": deck_id,
            "cards": len(card_ids),
            "rejected": generated.rejected,
        }

    def list_decks(self) -> list[dict]:
        return store.list_decks(self.conn, self.user_id)

    def get_deck(self, deck_id: int) -> Optional[dict]:
        return store.get_deck(self.conn, deck_id, self.user_id)

    def list_cards(self, deck_id: Optional[int] = None, due_only: bool = False) -> list[dict]:
        if deck_id is not None:
            assert_owned(self.conn, "decks", deck_id, self.user_id)
        return store.list_cards(
            self.conn, self.user_id, deck_id=deck_id, due_only=due_only
        )

    def review_card(self, card_id: int, grade: int) -> dict:
        """Grade a card and schedule its next appearance."""
        assert_owned(self.conn, "cards", card_id, self.user_id)
        card = store.get_card(self.conn, card_id, self.user_id)
        if card is None:  # pragma: no cover - assert_owned already raised
            raise NotFoundError("cards", card_id)

        interval, ease, due_at = cards_module.schedule(
            grade=grade,
            interval_days=int(card["interval_days"]),
            ease=int(card["ease"]),
            reviews=int(card["reviews"]),
        )
        store.record_review(
            self.conn, self.user_id, card_id, grade,
            interval_days=interval, ease=ease, due_at=due_at,
            lapsed=grade == cards_module.FORGOT,
        )
        return {
            "card_id": card_id,
            "interval_days": interval,
            "ease": ease,
            "due_at": due_at.isoformat(timespec="seconds"),
        }

    def build_test(self, deck_id: int, kind: str = "multiple_choice", length: int = 10):
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        deck_cards = store.list_cards(self.conn, self.user_id, deck_id=deck_id)
        return cards_module.build_test(deck_cards, kind=kind, length=length)

    def delete_deck(self, deck_id: int) -> None:
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        store.delete_deck(self.conn, deck_id, self.user_id)

    def deck_stats(self, deck_id: int) -> dict:
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        return store.deck_stats(self.conn, self.user_id, deck_id)

    def sync_note_cards(self, note_id: int, title: str, body: str) -> dict:
        """Keep a note's own deck in step with the cards its text declares.

        Runs on every save. Costs one parse and, when nothing changed, a
        handful of no-op updates -- cheap enough that making it a background
        job would add a window where the note and its cards disagree for no
        benefit anyone can perceive.
        """
        return store.sync_note_cards(
            self.conn, self.user_id, note_id,
            outline_module.to_cards(body), title=title or "Untitled",
        )

    def card_performance(self, deck_id: Optional[int] = None) -> list[dict]:
        if deck_id is not None:
            assert_owned(self.conn, "decks", deck_id, self.user_id)
        return store.card_performance(self.conn, self.user_id, deck_id=deck_id)

    def needs_practice(self, deck_id: Optional[int] = None, limit: int = 10) -> list[dict]:
        return progress_module.needs_practice(
            self.card_performance(deck_id=deck_id), limit=limit
        )

    def progress(self, deck_id: Optional[int] = None, days: int = 30) -> dict:
        return progress_module.summarise(
            self.card_performance(deck_id=deck_id),
            store.review_activity(self.conn, self.user_id, days=days),
        )

    # ---------------------------------------------------------------- evaluation

    def list_labels(self) -> list[dict]:
        return store.list_labels(self.conn, self.user_id)

    def set_label(
        self, assignment_id: int, note_id: int, relevant: bool, rationale: str = ""
    ) -> None:
        assert_owned(self.conn, "assignments", assignment_id, self.user_id)
        assert_owned(self.conn, "notes", note_id, self.user_id)
        store.set_label(
            self.conn, assignment_id, note_id, relevant, rationale, user_id=self.user_id
        )

    def load_eval_labels(self, path: Optional[Path] = None) -> int:
        pairs = load_labels(path or self.settings.labels_path)
        return sync_to_db(self.conn, pairs, self.user_id)

    def evaluate(self, config: Optional[RetrievalConfig] = None) -> EvalReport:
        return evaluate_config(
            self.conn,
            self.provider,
            config or self.config,
            self.settings.labels_path,
            user_id=self.user_id,
        )

    def sweep(self, **kwargs) -> list[EvalReport]:
        reports = sweep(
            self.conn, self.provider, self.settings.labels_path,
            base=self.config, user_id=self.user_id, **kwargs
        )
        # The sweep leaves the index chunked under whichever config ran last;
        # restore the active configuration so the app is not left inconsistent.
        self.reindex()
        return reports

    # -------------------------------------------------------------------- status

    def status(self) -> Status:
        counts = {
            name: int(
                self.conn.execute(
                    select(func.count()).select_from(table).where(table.c.user_id == self.user_id)
                ).scalar_one()
            )
            for name, table in (
                ("courses", schema.courses),
                ("assignments", schema.assignments),
                ("notes", schema.notes),
                ("chunks", schema.chunks),
            )
        }
        chunk_vectors = self.indexer.vectors.count("chunk", self.provider.name, self.user_id)
        assignment_vectors = self.indexer.vectors.count(
            "assignment", self.provider.name, self.user_id
        )

        params = store.chunking_params_in_use(self.conn, self.user_id)
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
            last_sync=store.last_sync(self.conn, self.user_id),
            index_stale=index_stale,
        )
