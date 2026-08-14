"""StudyLink -- connect Canvas assignments to your own notes.

Layering, bottom up:

    db / store        SQLite schema and CRUD
    chunking          note text -> embeddable chunks
    embeddings        pluggable embedding providers
    vectorstore       vector persistence and exact cosine search
    indexing          keeps chunks and vectors in sync with the config
    retrieval         note <-> assignment matching, with explanations
    agent             work-session synthesis with note citations
    evaluation        labelled set, metrics, LLM judge, config sweep
    service           the facade the UI, API, and scripts all drive
"""

__version__ = "0.1.0"
