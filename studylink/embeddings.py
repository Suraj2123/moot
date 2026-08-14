"""Embedding providers.

Three providers, one interface. The point of the abstraction is not
future-proofing for its own sake -- it is that the retrieval evaluation needs to
compare providers on the same labelled set, and swapping providers must not mean
rewriting the indexer.

  hash                   Deterministic hashed bag-of-ngrams. No network, no
                         dependencies beyond numpy. This is a *lexical* baseline,
                         not a semantic model: it matches on shared vocabulary.
                         It exists so the app and the eval run end-to-end with no
                         API keys, and so there is a floor to beat.
  voyage                 Hosted embedding API (Anthropic's recommended pairing).
  sentence-transformers  Local neural embeddings; free, no network at query time,
                         but pulls in torch.

Every vector is L2-normalised on the way out, so cosine similarity is a plain
dot product everywhere downstream.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence

import numpy as np

_TOKEN = re.compile(r"[a-z0-9]+")

# Small, hand-picked stoplist. A full stoplist would be overkill: the hash
# provider only needs the highest-frequency words removed so that shared
# boilerplate ("the assignment is due") does not dominate similarity.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "from", "has", "have", "how", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "their", "then", "there", "these", "they", "this", "to",
    "was", "were", "what", "when", "which", "will", "with", "you", "your",
}


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stoplisted, with naive plural folding.

    Shared by the hash provider and by the explainability code that reports which
    terms a note and an assignment have in common -- they should agree on what a
    "term" is.
    """
    tokens = []
    for raw in _TOKEN.findall((text or "").lower()):
        if raw in _STOPWORDS or len(raw) < 2:
            continue
        # Fold trivial plurals so "gradient"/"gradients" count as one term.
        if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss"):
            raw = raw[:-1]
        tokens.append(raw)
    return tokens


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (len(texts), dim) float32 array of L2-normalised vectors."""


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class HashingEmbeddingProvider:
    """Hashing-trick bag of unigrams+bigrams with sublinear term frequency.

    Deterministic and offline. Similarity here is vocabulary overlap, weighted so
    that a term repeated ten times does not count ten times as much.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _hash(self, term: str) -> int:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = tokenize(text)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for first, second in zip(tokens, tokens[1:]):
                bigram = f"{first}_{second}"
                counts[bigram] = counts.get(bigram, 0) + 1
            for term, count in counts.items():
                # Sublinear tf: repeated terms add signal, but with diminishing
                # returns, which keeps long notes from swamping short ones.
                out[row, self._hash(term)] += 1.0 + math.log(count)
        return _l2_normalize(out)


class VoyageEmbeddingProvider:
    """Voyage AI hosted embeddings, called over plain HTTP."""

    # Voyage rejects oversized batches; 96 is comfortably inside the limit.
    BATCH = 96
    ENDPOINT = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str = "voyage-3") -> None:
        if not api_key:
            raise ValueError("VOYAGE_API_KEY is required for the voyage provider")
        self._api_key = api_key
        self.name = model
        self.dim = 1024  # voyage-3 default; corrected from the first response

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        import requests

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH):
            batch = list(texts[start : start + self.BATCH])
            response = requests.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"input": batch, "model": self.name},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            # The API returns items with an `index` field; sort rather than
            # assuming the response preserves request order.
            items = sorted(payload["data"], key=lambda d: d["index"])
            vectors.extend(item["embedding"] for item in items)

        if vectors:
            self.dim = len(vectors[0])
        return _l2_normalize(np.array(vectors, dtype=np.float32))


class SentenceTransformerProvider:
    """Local neural embeddings via sentence-transformers."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model)
        self.name = model
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self._model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)
        return _l2_normalize(np.asarray(vectors, dtype=np.float32))


def build_provider(provider: str, model: str = "", voyage_api_key: str = "") -> EmbeddingProvider:
    """Construct a provider by name. Raises rather than silently downgrading."""
    provider = (provider or "hash").lower()

    if provider == "hash":
        dim = 256
        if model and model.startswith("hash-"):
            dim = int(model.split("-", 1)[1])
        return HashingEmbeddingProvider(dim=dim)

    if provider == "voyage":
        return VoyageEmbeddingProvider(api_key=voyage_api_key, model=model or "voyage-3")

    if provider in ("sentence-transformers", "sbert", "st"):
        return SentenceTransformerProvider(model=model or "sentence-transformers/all-MiniLM-L6-v2")

    raise ValueError(
        f"Unknown embedding provider {provider!r}. "
        "Expected one of: hash, voyage, sentence-transformers."
    )
