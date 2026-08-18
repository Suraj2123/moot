import { useEffect, useState } from "react";
import { notes, jobs, ApiError, type Job, type Note, type Match } from "../api";
import { Alert, Empty, Skeleton, ConfidenceBadge, ScoreBar } from "../components/ui";
import { IconPlus, IconSearch } from "../components/Icons";

export function NotesPage() {
  const [items, setItems] = useState<Note[] | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [composing, setComposing] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  async function load(term = search) {
    try {
      setItems(await notes.list(term));
      setError("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load notes.");
    }
  }

  useEffect(() => { load(""); }, []);

  // Debounced so typing does not fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => load(search), 220);
    return () => clearTimeout(t);
  }, [search]);

  return (
    <div className="content-inner">
      <div className="page-head between">
        <div>
          <h1>Notes</h1>
          <p>Everything you have written, and what each note is relevant to.</p>
        </div>
        <button className="btn btn-primary" onClick={() => setComposing((v) => !v)}>
          <IconPlus /> New note
        </button>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {composing ? (
        <NoteComposer
          onDone={() => { setComposing(false); load(); }}
          onCancel={() => setComposing(false)}
        />
      ) : null}

      <div className="field" style={{ position: "relative" }}>
        <span style={{ position: "absolute", left: 11, top: 10, color: "var(--text-faint)" }}>
          <IconSearch />
        </span>
        <input
          className="input"
          style={{ paddingLeft: 34 }}
          placeholder="Search your notes"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {items === null ? (
        <Skeleton count={4} />
      ) : items.length === 0 ? (
        <Empty title={search ? "No notes match that" : "No notes yet"}>
          {search
            ? "Try a different word, or clear the search."
            : "Add your first note and StudyLink will match it to your assignments."}
        </Empty>
      ) : (
        <div className="stack">
          {items.map((note) => (
            <div key={note.id}>
              <div className="note-item" onClick={() => setOpen(open === note.id ? null : note.id)}>
                <div className="between">
                  <div style={{ minWidth: 0 }}>
                    <h3>{note.title}</h3>
                    <div className="small faint">
                      {note.course ?? "Unassigned"} · {note.chars.toLocaleString()} characters
                      {note.source_type === "transcript" ? " · transcript" : ""}
                    </div>
                  </div>
                  <span className="badge">{open === note.id ? "Hide" : "Related"}</span>
                </div>
              </div>
              {open === note.id ? <RelatedAssignments noteId={note.id} /> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NoteComposer({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [queued, setQueued] = useState<number | null>(null);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await notes.create(title.trim(), body.trim());
      // Indexing runs in the background, so the note is saved but not yet
      // searchable. Saying so beats a spinner that implies otherwise.
      setQueued(result.job?.id ?? null);
      setTimeout(onDone, 900);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save that note.");
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      {error ? <Alert>{error}</Alert> : null}
      {queued !== null ? (
        <Alert kind="info">Saved. Indexing runs in the background — it will be searchable shortly.</Alert>
      ) : null}

      <div className="field">
        <label htmlFor="note-title">Title</label>
        <input
          id="note-title" className="input" autoFocus
          value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder="Lecture 4 — gradient descent"
        />
      </div>
      <div className="field">
        <label htmlFor="note-body">Note</label>
        <textarea
          id="note-body" className="textarea" style={{ minHeight: 190 }}
          value={body} onChange={(e) => setBody(e.target.value)}
          placeholder="Paste or write your notes here. Markdown and plain text both work."
        />
      </div>
      <div className="row">
        <button className="btn btn-primary" onClick={save} disabled={busy || !title.trim() || !body.trim()}>
          Save note
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </div>
  );
}

function RelatedAssignments({ noteId }: { noteId: number }) {
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    notes.assignmentsFor(noteId)
      .then(setMatches)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load matches."));
  }, [noteId]);

  if (error) return <div style={{ padding: "10px 4px" }}><Alert>{error}</Alert></div>;
  if (matches === null) return <div style={{ padding: "10px 4px" }}><Skeleton count={2} height={44} /></div>;
  if (matches.length === 0) {
    return (
      <p className="small faint" style={{ padding: "10px 4px 4px" }}>
        Nothing matched this note yet. If you just saved it, indexing may still be running.
      </p>
    );
  }

  return (
    <div className="stack" style={{ padding: "10px 0 4px 14px" }}>
      {matches.map((m) => (
        <div className="match" key={m.assignment_id}>
          <div className="match-head">
            <div style={{ minWidth: 0 }}>
              <strong style={{ fontSize: 13.5 }}>{m.name}</strong>
              <div className="small faint">{m.course}</div>
            </div>
            <div className="row">
              <ScoreBar score={m.score} />
              <ConfidenceBadge confidence={m.confidence} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function JobsStrip() {
  const [items, setItems] = useState<Job[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = () => jobs.list().then((j) => alive && setItems(j.slice(0, 3))).catch(() => {});
    tick();
    const t = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!items.some((j) => j.status === "queued" || j.status === "running")) return null;
  return <span className="badge badge-accent">Indexing…</span>;
}

