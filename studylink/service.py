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
from . import credentials, modelkeys, schema, store
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
from .vectorstore import PublicDeckIndex


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
        folder_id: Optional[int] = None,
    ) -> int:
        if folder_id is not None:
            assert_owned(self.conn, "folders", folder_id, self.user_id)
        note_id = store.create_note(
            self.conn, title, body, course_id, source_type, self.user_id,
            folder_id=folder_id,
        )
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

    # How many unindexed notes this request will index itself rather than
    # leaving for the worker. A student who has just written their first few
    # notes and typed their first target is the whole audience for this
    # feature, and telling them "no matches, come back in a minute" is
    # indistinguishable from telling them it does not work. Someone who has
    # just uploaded two hundred PDFs has a worker for that, and should not
    # discover the difference as a thirty-second POST.
    INLINE_INDEX_LIMIT = 25

    def pending_notes(self) -> int:
        """Notes that retrieval cannot see yet.

        Counted over the notes rather than over the status map, because that
        map is built from chunk rows and so has no entry at all for a note
        that has never been chunked -- which is exactly the case this is for.
        A note absent from it is the most unindexed a note can be.
        """
        status = store.note_index_status(
            self.conn, self.user_id,
            model=self.provider.name,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )
        return sum(
            1
            for note in store.list_notes(self.conn, self.user_id)
            if status.get(note.id) != "indexed"
        )

    def catch_up_index(self) -> int:
        """Index a small backlog now; leave a large one to the worker."""
        pending = self.pending_notes()
        if 0 < pending <= self.INLINE_INDEX_LIMIT:
            self.reindex()
        return pending

    # ----------------------------------------------------------------- folders

    def list_folders(self) -> list[dict]:
        return store.list_folders(self.conn, self.user_id)

    def create_folder(self, name: str) -> dict:
        folder_id = store.create_folder(self.conn, self.user_id, name)
        return {**store.get_folder(self.conn, folder_id, self.user_id), "notes": 0}

    def rename_folder(self, folder_id: int, name: str) -> dict:
        assert_owned(self.conn, "folders", folder_id, self.user_id)
        return store.rename_folder(self.conn, folder_id, self.user_id, name)

    def delete_folder(self, folder_id: int) -> None:
        """Remove a folder; its notes survive, unfiled."""
        assert_owned(self.conn, "folders", folder_id, self.user_id)
        store.delete_folder(self.conn, folder_id, self.user_id)

    def move_note(self, note_id: int, folder_id: Optional[int]) -> None:
        assert_owned(self.conn, "notes", note_id, self.user_id)
        if folder_id is not None:
            assert_owned(self.conn, "folders", folder_id, self.user_id)
        store.set_note_folder(self.conn, note_id, self.user_id, folder_id)

    # ------------------------------------------------------------ study targets

    def create_target(
        self,
        name: str,
        description: str = "",
        due_at: Optional[str] = None,
    ) -> Assignment:
        """Something to study for, and the matched notes, in one call.

        Embedded immediately rather than left for the worker. The whole value
        of typing a topic is seeing which of your notes bear on it *now*, and a
        target that matches nothing for thirty seconds reads as a feature that
        does not work rather than one that is still indexing.
        """
        self.catch_up_index()
        target_id = store.create_target(
            self.conn, self.user_id, name=name, description=description, due_at=due_at
        )
        target = store.get_assignment(self.conn, target_id, self.user_id)
        vector = self.provider.embed([target.retrieval_text])[0]
        self.indexer.vectors.upsert_many(
            "assignment", [target_id], vector.reshape(1, -1), self.provider.name
        )
        return target

    def update_target(self, target_id: int, **fields) -> Assignment:
        assert_owned(self.conn, "assignments", target_id, self.user_id)
        if store.assignment_source(self.conn, target_id, self.user_id) != "manual":
            raise CanvasError(
                "This came from Canvas, so editing it here would be undone by "
                "the next sync. Change it in Canvas, or make a target of your "
                "own."
            )
        target = store.update_target(self.conn, target_id, self.user_id, **fields)
        # The text it matches on has changed, so the vector describing it has to.
        vector = self.provider.embed([target.retrieval_text])[0]
        self.indexer.vectors.upsert_many(
            "assignment", [target_id], vector.reshape(1, -1), self.provider.name
        )
        return target

    def delete_target(self, target_id: int) -> None:
        assert_owned(self.conn, "assignments", target_id, self.user_id)
        if store.assignment_source(self.conn, target_id, self.user_id) != "manual":
            raise CanvasError(
                "This came from Canvas. Disconnect Canvas or remove it there -- "
                "deleting it here would only bring it back on the next sync."
            )
        self.indexer.vectors.delete("assignment", [target_id])
        store.delete_target(self.conn, target_id, self.user_id)

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
        self, note_id: int, count: Optional[int] = None,
        title: Optional[str] = None, writer=None,
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

        own_key = modelkeys.get_key(self.conn, self.user_id)
        if own_key is None:
            # The allowance exists to cap what the operator pays for. Someone
            # spending their own key is not spending it, so there is nothing
            # for the cap to protect and it does not apply -- which is the
            # entire point of letting them bring one.
            usage_module.check_budget(self.conn, self.user_id)

        writer = writer or cards_module.CardWriter(api_key=own_key)
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

    # --------------------------------------------------- writing cards by hand

    def create_empty_deck(self, title: str) -> dict:
        """A deck with a title and no cards yet, for typing them in.

        The third way to get a deck, alongside a note declaring them and the
        model writing them -- and the one that needs neither a note nor a key.
        """
        title = (title or "").strip() or "Untitled deck"
        deck_id = store.create_deck(self.conn, self.user_id, title=title)
        return {**store.get_deck(self.conn, deck_id, self.user_id), "cards": 0, "due": 0}

    def rename_deck(self, deck_id: int, title: str) -> dict:
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        return store.rename_deck(
            self.conn, deck_id, self.user_id, (title or "").strip() or "Untitled deck"
        )

    def add_cards(self, deck_id: int, pairs: list[tuple[str, str]]) -> list[dict]:
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        ids = store.write_cards(self.conn, self.user_id, deck_id, pairs)
        self._reindex_if_published(deck_id)
        return [store.get_card(self.conn, card_id, self.user_id) for card_id in ids]

    def edit_card(
        self, card_id: int, front: Optional[str] = None, back: Optional[str] = None
    ) -> dict:
        """Change a card's text, unless a note is the one writing it."""
        assert_owned(self.conn, "cards", card_id, self.user_id)
        self._refuse_if_note_writes_it(card_id, "edited")
        card = store.update_card(self.conn, card_id, self.user_id, front, back)
        self._reindex_if_published(int(card["deck_id"]))
        return card

    def remove_card(self, card_id: int) -> None:
        assert_owned(self.conn, "cards", card_id, self.user_id)
        self._refuse_if_note_writes_it(card_id, "deleted")
        card = store.get_card(self.conn, card_id, self.user_id)
        store.delete_card(self.conn, card_id, self.user_id)
        if card:
            self._reindex_if_published(int(card["deck_id"]))

    def _refuse_if_note_writes_it(self, card_id: int, verb: str) -> None:
        """Stop an edit that the next note save would silently undo.

        A card declared by `::` in a note is re-derived from that text every
        time the note is saved. Accepting an edit here and reverting it an hour
        later is worse than refusing now, because the student has no way to
        find out it happened -- so this says where the real text lives instead.
        """
        origin = store.card_origin(self.conn, card_id, self.user_id)
        if origin and origin["derived"]:
            where = f' "{origin["note_title"]}"' if origin["note_title"] else ""
            raise cards_module.CardError(
                f"This card is written by your note{where}, so it cannot be "
                f"{verb} here -- the next time you save that note it would come "
                "back. Change the line in the note and the card follows."
            )

    def _reindex_if_published(self, deck_id: int) -> None:
        """Keep a public deck's search text in step with cards typed by hand."""
        deck = store.get_deck(self.conn, deck_id, self.user_id)
        if deck and deck["visibility"] == "public":
            self.index_public_deck(deck_id)

    # ---------------------------------------------------------------- sharing

    def set_deck_visibility(self, deck_id: int, visibility: str) -> dict:
        """Publish, unlist, or withdraw a deck, and keep the search index honest.

        The index update is not a background job. A student who makes a deck
        private has withdrawn consent, and "it stops being findable within a
        minute" is not what withdrawing consent means -- so the vector goes at
        the same moment as the flag, in the same request.
        """
        assert_owned(self.conn, "decks", deck_id, self.user_id)
        deck = store.set_deck_visibility(self.conn, deck_id, self.user_id, visibility)

        index = PublicDeckIndex(self.conn)
        if visibility == "public":
            self.index_public_deck(deck_id)
        else:
            index.forget(deck_id)
        return deck

    def index_public_deck(self, deck_id: int) -> bool:
        """Embed a public deck so it can be found. Returns whether it indexed.

        Uses the same provider as everything else, which on the default
        configuration is the offline hashing one -- so deck discovery works
        with no API key, like every other core feature.
        """
        text = store.deck_text_for_search(self.conn, deck_id)
        if not text.strip():
            return False
        vector = self.provider.embed([text])[0]
        PublicDeckIndex(self.conn).upsert(deck_id, vector, self.provider.name)
        return True

    def shared_deck(self, slug: str) -> Optional[dict]:
        """A deck by slug for any reader, signed in or not."""
        return store.get_shared_deck(self.conn, slug)

    def fork_deck(self, slug: str) -> Optional[dict]:
        return store.fork_deck(self.conn, slug, self.user_id)

    def discover_decks(self, query: str = "", limit: int = 20) -> list[dict]:
        """Public decks, ranked by a query or listed newest-first without one.

        An empty query is not an empty result. Someone who opens the page
        before typing anything should see that decks exist at all.
        """
        query = (query or "").strip()
        if not query:
            return store.public_decks(self.conn)[:limit]

        vector = self.provider.embed([query])[0]
        hits = PublicDeckIndex(self.conn).search(
            vector, self.provider.name, top_k=limit
        )
        # A cosine search always returns its top k, however badly they match.
        # Without a floor, searching "photosynthesis" on a service holding one
        # deck about Roman history returns that deck, and the page has just
        # told a lie about what it found. The same threshold the retriever
        # uses, for the same reason.
        floor = self.config.score_threshold
        hits = [(deck_id, score) for deck_id, score in hits if score >= floor]
        if not hits:
            return []

        scores = {deck_id: score for deck_id, score in hits}
        found = store.public_decks(self.conn, deck_ids=list(scores))
        for deck in found:
            deck["score"] = round(scores.get(deck["id"], 0.0), 4)
        # Ranked by the search, not by the store's newest-first ordering.
        return sorted(found, key=lambda d: -d["score"])

    def sync_note_cards(self, note_id: int, title: str, body: str) -> dict:
        """Keep a note's own deck in step with the cards its text declares.

        Runs on every save. Costs one parse and, when nothing changed, a
        handful of no-op updates -- cheap enough that making it a background
        job would add a window where the note and its cards disagree for no
        benefit anyone can perceive.
        """
        result = store.sync_note_cards(
            self.conn, self.user_id, note_id,
            outline_module.to_cards(body), title=title or "Untitled",
        )

        # A published deck's search text is its cards, so editing the note that
        # owns it makes the index describe a deck that no longer exists in that
        # shape. Re-embedding here rather than on the next publish is the
        # difference between search finding the deck as it is and finding it as
        # it was.
        deck_id = result.get("deck_id")
        if deck_id and (result["added"] or result["updated"] or result["removed"]):
            deck = store.get_deck(self.conn, deck_id, self.user_id)
            if deck and deck["visibility"] == "public":
                self.index_public_deck(deck_id)
        return result

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
