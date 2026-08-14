# StudyLink

A notes app that connects to Canvas, automatically maps your notes and lecture material to the assignments they're relevant to, and helps you turn matched notes into a working draft or study plan — powered by retrieval (RAG) and an AI agent, not just manual searching.

## Why

Notes end up scattered across a semester — different courses, different formats, no link back to the assignment they actually apply to. StudyLink closes that gap: pull assignments from Canvas, embed your notes, semantically match them, evaluate how good the matching actually is, and use an agent to synthesize matched notes + assignment context into something you can start working from.

## Features

- **Canvas integration** — pulls assignments, descriptions, and due dates via the Canvas API
- **Notes ingestion** — upload or paste notes (markdown/plain text), tagged by course
- **Semantic matching** — embeds notes and assignments, retrieves the most relevant notes for a given assignment (with explainability — you can see *why* a note matched)
- **Retrieval evaluation** — a labeled eval set and precision@k / LLM-as-judge scoring to measure and improve match quality, not just assume it works
- **Agent-powered work sessions** — select an assignment, get matched notes pulled automatically, and an agent synthesizes an outline, draft skeleton, or summary — with citations back to the source notes, and a chat interface to refine it
- **Simple dashboard UI** — course view, notes view, and per-assignment work sessions

## Tech Stack

| Layer | Tool |
|---|---|
| Canvas integration | Canvas REST API |
| Embeddings | OpenAI / sentence-transformers / Voyage |
| Vector store | Chroma / FAISS |
| Agent layer | CrewAI / LangGraph |
| Backend | Python (FastAPI) |
| Frontend | Streamlit |
| Storage | SQLite |

## Getting Started

```bash
git clone https://github.com/Suraj2123/studylink.git
cd studylink
pip install -r requirements.txt
cp .env.example .env   # add your Canvas API token and embedding API key
python app.py
```

### Environment Variables

```
CANVAS_API_URL=
CANVAS_API_TOKEN=
EMBEDDING_API_KEY=
```

## Project Status

🚧 In active development — built as a hands-on project alongside coursework in generative AI, LLM evaluation, RAG, and AI agents.

**Roadmap:**
- [x] Canvas API integration (assignments + course data)
- [ ] Notes ingestion pipeline
- [ ] Embedding + semantic retrieval
- [ ] Retrieval evaluation (labeled set + metrics)
- [ ] Agent-based work session synthesis
- [ ] Dashboard UI
- [ ] Lecture transcript ingestion (stretch)
- [ ] Google Classroom integration (stretch)

## License

MIT
