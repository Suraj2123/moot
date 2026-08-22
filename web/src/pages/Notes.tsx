import { useEffect, useRef, useState } from "react";
import {
  notes, jobs, ApiError, uploadNote, UPLOAD_ACCEPT, UPLOAD_MAX_BYTES,
  type Job, type Note, type Match,
} from "../api";
import { Alert, Empty, Skeleton, ConfidenceBadge, ScoreBar } from "../components/ui";
import { OutlineEditor } from "../components/OutlineEditor";
import { IconPlus, IconSearch, IconUpload } from "../components/Icons";

export function NotesPage() {
  const [items, setItems] = useState<Note[] | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [composing, setComposing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);

  async function remove(note: Note) {
    if (!window.confirm(`Delete "${note.title}"? This cannot be undone.`)) return;

    // Optimistic: the row goes immediately, and comes back if the server
    // disagrees. Deleting is the one action where waiting feels broken --
    // you already know what you wanted to happen.
    const before = items;
    setItems((prev) => prev?.filter((n) => n.id !== note.id) ?? prev);
    setDeleting(note.id);
    try {
      await notes.remove(note.id);
      setError("");
    } catch (err) {
      setItems(before);
      setError(err instanceof ApiError ? err.message : "Could not delete that note.");
    } finally {
      setDeleting(null);
    }
  }

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
        <div className="row">
          <button className="btn btn-ghost" onClick={() => setUploading((v) => !v)}>
            <IconUpload /> Upload
          </button>
          <button className="btn btn-primary" onClick={() => setComposing((v) => !v)}>
            <IconPlus /> New note
          </button>
        </div>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {uploading ? (
        <Uploader onDone={() => load()} onCancel={() => setUploading(false)} />
      ) : null}

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
            : "Add your first note and moot will match it to your assignments."}
        </Empty>
      ) : (
        <div className="stack">
          {items.map((note) => (
            <div key={note.id}>
              {editing === note.id ? (
                <NoteEditor
                  note={note}
                  onCancel={() => setEditing(null)}
                  onSaved={() => { setEditing(null); load(); }}
                />
              ) : (
                <div className="note-item" onClick={() => setOpen(open === note.id ? null : note.id)}>
                  <div className="between">
                    <div style={{ minWidth: 0 }}>
                      <h3>{note.title}</h3>
                      <div className="small faint">
                        {/* `||`, not `??`: an unassigned note comes back with an
                            empty course name rather than null, which `??` passes
                            straight through and renders as a stray separator. */}
                        {note.course || "Unassigned"} · {note.chars.toLocaleString()} characters
                        {note.source_type === "transcript" ? " · transcript" : ""}
                      </div>
                    </div>
                    <div className="row" onClick={(e) => e.stopPropagation()}>
                      <IndexBadge status={note.index_status} />
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => { setEditing(note.id); setOpen(null); }}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-ghost btn-sm danger"
                        onClick={() => remove(note)}
                        disabled={deleting === note.id}
                      >
                        {deleting === note.id ? "Deleting…" : "Delete"}
                      </button>
                      <span
                        className="badge"
                        onClick={() => setOpen(open === note.id ? null : note.id)}
                        style={{ cursor: "pointer" }}
                      >
                        {open === note.id ? "Hide" : "Related"}
                      </span>
                    </div>
                  </div>
                </div>
              )}
              {open === note.id ? <RelatedAssignments noteId={note.id} /> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Whether retrieval can see this note yet.
 *
 * Worth showing because the alternative is silent: a note saves, does not
 * match anything for a few seconds, and looks broken. Naming the state turns
 * a bug report into a wait.
 */
function IndexBadge({ status }: { status?: Note["index_status"] }) {
  if (!status || status === "indexed") return null;
  return (
    <span className="badge" title={
      status === "queued"
        ? "Waiting for the indexer. It will be searchable shortly."
        : "Not indexed yet. Start the worker to process the queue."
    }>
      {status === "queued" ? "Indexing…" : "Not indexed"}
    </span>
  );
}

function NoteEditor({
  note, onCancel, onSaved,
}: { note: Note; onCancel: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // The list omits bodies, so the text has to be fetched before it can be
  // edited. Until it arrives the textarea stays disabled rather than showing
  // an empty box someone might type into and save over the real note.
  useEffect(() => {
    let alive = true;
    notes.get(note.id)
      .then((full) => alive && setBody(full.body))
      .catch((err) => alive && setError(
        err instanceof ApiError ? err.message : "Could not load that note.",
      ));
    return () => { alive = false; };
  }, [note.id]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      await notes.update(note.id, { title: title.trim(), body: body ?? undefined });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save that note.");
      setBusy(false);
    }
  }

  return (
    <div className="card">
      {error ? <Alert>{error}</Alert> : null}
      <div className="field">
        <label htmlFor={`edit-title-${note.id}`}>Title</label>
        <input
          id={`edit-title-${note.id}`} className="input" autoFocus
          value={title} onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor={`edit-body-${note.id}`}>Note</label>
        <OutlineEditor
          id={`edit-body-${note.id}`}
          value={body ?? ""}
          disabled={body === null}
          placeholder={body === null ? "Loading…" : ""}
          onChange={setBody}
          minHeight={200}
        />
      </div>
      <div className="row">
        <button
          className="btn btn-primary"
          onClick={save}
          disabled={busy || body === null || !title.trim()}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
        <span className="small faint">
          Editing the text re-indexes this note; renaming does not.
        </span>
      </div>
    </div>
  );
}

/** One row per file, so a failure names the file it belongs to. */
interface Upload {
  file: File;
  status: "pending" | "uploading" | "done" | "error";
  message?: string;
}

function Uploader({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [rows, setRows] = useState<Upload[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  // Nested drag events fire on every child element, so a plain
  // enter/leave pair flickers the highlight. Counting depth fixes it.
  const depth = useRef(0);

  function accept(list: FileList | null) {
    if (!list?.length) return;
    setRows((prev) => [
      ...prev,
      ...Array.from(list).map((file): Upload => {
        // Checked here as well as on the server, because a 10 MB round trip
        // to be told no is a poor way to learn the limit.
        if (file.size > UPLOAD_MAX_BYTES) {
          return {
            file,
            status: "error",
            message: `${(file.size / 1048576).toFixed(1)} MB is over the 10 MB limit.`,
          };
        }
        return { file, status: "pending" };
      }),
    ]);
  }

  async function send() {
    setBusy(true);
    // Sequential, not Promise.all: each upload queues an indexing job, and
    // firing ten at once buries the worker for no gain on a corpus this size.
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].status !== "pending") continue;
      setRows((prev) => prev.map((r, j) => (i === j ? { ...r, status: "uploading" } : r)));
      try {
        const result = await uploadNote(rows[i].file);
        setRows((prev) => prev.map((r, j) => (i === j ? {
          ...r,
          status: "done",
          message: result.notice ?? `${result.chars.toLocaleString()} characters`,
        } : r)));
        onDone();
      } catch (err) {
        const message = err instanceof ApiError ? err.message : "Upload failed.";
        setRows((prev) => prev.map((r, j) => (i === j ? { ...r, status: "error", message } : r)));
      }
    }
    setBusy(false);
  }

  const pending = rows.filter((r) => r.status === "pending").length;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div
        className={`dropzone${dragging ? " dragging" : ""}`}
        onDragEnter={(e) => { e.preventDefault(); depth.current += 1; setDragging(true); }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={(e) => {
          e.preventDefault();
          depth.current -= 1;
          if (depth.current <= 0) { depth.current = 0; setDragging(false); }
        }}
        onDrop={(e) => {
          e.preventDefault();
          depth.current = 0;
          setDragging(false);
          accept(e.dataTransfer.files);
        }}
        onClick={() => input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
      >
        <IconUpload />
        <strong>Drop files here, or click to choose</strong>
        <span className="small faint">PDF, Word, Markdown, or plain text · up to 10 MB each</span>
        <input
          ref={input}
          type="file"
          multiple
          accept={UPLOAD_ACCEPT}
          style={{ display: "none" }}
          onChange={(e) => { accept(e.target.files); e.target.value = ""; }}
        />
      </div>

      {rows.length > 0 ? (
        <div className="stack" style={{ marginTop: 12 }}>
          {rows.map((row, i) => (
            <div className="upload-row" key={`${row.file.name}-${i}`}>
              <div style={{ minWidth: 0 }}>
                <div className="upload-name">{row.file.name}</div>
                {row.message ? (
                  <div className={`small ${row.status === "error" ? "danger" : "faint"}`}>
                    {row.message}
                  </div>
                ) : null}
              </div>
              <span className={`badge${row.status === "done" ? " badge-accent" : ""}`}>
                {row.status === "uploading" ? "Reading…"
                  : row.status === "done" ? "Added"
                  : row.status === "error" ? "Failed"
                  : "Ready"}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn btn-primary" onClick={send} disabled={busy || pending === 0}>
          {busy ? "Uploading…" : pending > 1 ? `Add ${pending} files` : "Add file"}
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>Close</button>
      </div>

      {rows.some((r) => r.status === "done") ? (
        <p className="small faint" style={{ marginTop: 10, marginBottom: 0 }}>
          Indexing runs in the background — uploaded files become searchable shortly.
        </p>
      ) : null}
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
        <OutlineEditor
          id="note-body"
          value={body}
          onChange={setBody}
          minHeight={190}
          placeholder={"Write your notes as an outline.\n\nlearning rate :: controls the step size\nThe capital of France is {{Paris}}"}
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
