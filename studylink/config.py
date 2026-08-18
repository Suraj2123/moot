"""Configuration for StudyLink.

Everything that could plausibly differ between machines or between retrieval
experiments lives here. Secrets come from the environment only -- never from a
committed file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

try:  # optional convenience: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "studylink.db"
DEFAULT_LABELS_PATH = PROJECT_ROOT / "data" / "eval" / "labels.json"

# Anthropic model used for the work-session agent and the LLM judge.
AGENT_MODEL = "claude-opus-5"
JUDGE_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class RetrievalConfig:
    """The knobs the retrieval evaluation is allowed to tune.

    `chunk_size`/`chunk_overlap` are in words, not tokens. Words are a coarser
    unit than tokens but they are cheap to compute and stable across embedding
    providers, which matters because the eval sweeps over these values.
    """

    chunk_size: int = 180
    chunk_overlap: int = 40
    top_k: int = 5
    # Cosine similarity below which a match is not shown at all.
    score_threshold: float = 0.15
    # Similarity that should read as "clearly relevant" in the UI. Used to map
    # a raw cosine score onto a 0-1 confidence bar.
    confidence_ceiling: float = 0.65

    def with_(self, **kwargs) -> "RetrievalConfig":
        return replace(self, **kwargs)

    @property
    def label(self) -> str:
        return f"chunk={self.chunk_size}/{self.chunk_overlap} k={self.top_k} thr={self.score_threshold}"


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    labels_path: Path = DEFAULT_LABELS_PATH

    # SQLAlchemy URL. Empty means "derive a SQLite URL from db_path", which is
    # what local development and the test suite use. Setting DATABASE_URL to a
    # postgresql+psycopg2:// URL is the only change needed to run on Postgres.
    database_url: str = ""

    canvas_api_url: str = ""
    canvas_api_token: str = ""

    # "voyage" | "sentence-transformers" | "hash"
    embedding_provider: str = "hash"
    embedding_model: str = ""
    voyage_api_key: str = ""

    anthropic_api_key: str = ""

    retrieval: RetrievalConfig = RetrievalConfig()

    @property
    def sqlalchemy_url(self) -> str:
        """The URL to connect with, falling back to SQLite at `db_path`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"

    @property
    def is_postgres(self) -> bool:
        return self.sqlalchemy_url.startswith("postgres")

    @property
    def canvas_configured(self) -> bool:
        return bool(self.canvas_api_url and self.canvas_api_token)

    @property
    def anthropic_configured(self) -> bool:
        # The SDK also resolves credentials from an `ant auth login` profile, so
        # an unset env var does not always mean "no credentials".
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _normalize_database_url(url: str) -> str:
    """Accept the bare `postgres://` scheme that most hosts hand out.

    Railway, Heroku, and Render all export DATABASE_URL as `postgres://...`,
    but SQLAlchemy 2.x removed the `postgres` dialect alias in favour of
    `postgresql`, and drops the `psycopg2` driver unless it is named. Doing the
    substitution here means the platform's default variable reference works
    unedited, instead of failing at engine construction with a driver error.
    """
    url = url.strip()
    if not url:
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _default_embedding_provider() -> str:
    explicit = os.environ.get("EMBEDDING_PROVIDER", "").strip()
    if explicit:
        return explicit
    if os.environ.get("VOYAGE_API_KEY"):
        return "voyage"
    return "hash"


def load_settings() -> Settings:
    """Build settings from the environment.

    The defaults are chosen so that `python scripts/seed_demo.py` followed by
    `streamlit run app.py` works on a machine with no API keys at all: the
    `hash` embedding provider is deterministic, local, and dependency-free.
    """

    provider = _default_embedding_provider()
    default_model = {
        "voyage": "voyage-3",
        "sentence-transformers": "sentence-transformers/all-MiniLM-L6-v2",
        "hash": "hash-256",
    }.get(provider, "hash-256")

    return Settings(
        db_path=Path(os.environ.get("STUDYLINK_DB", str(DEFAULT_DB_PATH))),
        database_url=_normalize_database_url(os.environ.get("DATABASE_URL", "")),
        labels_path=Path(os.environ.get("STUDYLINK_LABELS", str(DEFAULT_LABELS_PATH))),
        canvas_api_url=os.environ.get("CANVAS_API_URL", "").rstrip("/"),
        canvas_api_token=os.environ.get("CANVAS_API_TOKEN", ""),
        embedding_provider=provider,
        embedding_model=os.environ.get("EMBEDDING_MODEL", default_model),
        voyage_api_key=os.environ.get("VOYAGE_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        retrieval=RetrievalConfig(
            chunk_size=int(os.environ.get("CHUNK_SIZE", 180)),
            chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", 40)),
            top_k=int(os.environ.get("TOP_K", 5)),
            score_threshold=float(os.environ.get("SCORE_THRESHOLD", 0.15)),
        ),
    )
