import { useEffect, useRef, useState } from "react";
import { api, cohorts, notes, type Cohort, type Note, type Source } from "../api";
import { Alert, Spinner } from "./ui";

export function CohortScope({ value, onChange, disabled = false }: { value: number | null; onChange: (id: number | null) => void; disabled?: boolean }) {
  const [items, setItems] = useState<Cohort[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { let alive = true; cohorts.list().then(x => { if (alive) setItems(x); }).catch(() => { if (alive) setError("Could not load cohorts. Private notes are still available."); }); return () => { alive = false; }; }, []);
  return <div className="cohort-scope">
    <label>Answer from <select className="input" aria-label="Answer from" value={value ?? ""} disabled={disabled} onChange={e => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">My private library</option>
      {items.map(c => <option key={c.id} value={c.id}>My notes + {c.name} · {c.term}</option>)}
    </select></label>
    <span className="small muted">Changing scope starts a new conversation.</span>
    {error && <Alert>{error}</Alert>}
  </div>;
}

export function ShareNoteControl({ note, items, onChanged }: { note: Note; items: Cohort[]; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<number | null>(note.shared_cohort_id ?? null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setTarget(note.shared_cohort_id ?? null); }, [note.shared_cohort_id]);
  async function save() {
    setBusy(true); setError("");
    try {
      if (name.trim()) await cohorts.profile(name.trim());
      await cohorts.share(note.id, target);
      setOpen(false); onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="note-sharing" onClick={e => e.stopPropagation()}>
    <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)} aria-expanded={open}>
      {note.visibility === "cohort" ? "Shared with cohort" : "Private · Share"}
    </button>
    {open && <div className="cohort-card stack">
      <p className="small muted">Only this note will be shared. Classmates can read it and cite your display name. You can make it private again at any time; existing answers remain.</p>
      <label>Visibility <select className="input" value={target ?? ""} onChange={e => setTarget(e.target.value ? Number(e.target.value) : null)} disabled={busy}>
        <option value="">Private — only me</option>
        {items.map(c => <option key={c.id} value={c.id}>{c.name} · {c.term}</option>)}
      </select></label>
      {!items.length && <p className="small muted">Create or join a cohort from the Cohorts screen first.</p>}
      {target !== null && <label>Display name <input className="input" maxLength={255} placeholder="Leave blank to keep your current name" value={name} onChange={e => setName(e.target.value)} disabled={busy} /></label>}
      {error && <Alert>{error}</Alert>}
      <div className="row"><button className="btn btn-primary btn-sm" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save sharing"}</button><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setOpen(false)}>Cancel</button></div>
    </div>}
  </div>;
}

export function SourceDialog({ source, onClose }: { source: Source; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [note, setNote] = useState<{title: string; body?: string; contributor_name?: string | null} | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    let alive = true;
    const read = source.cohort_id && source.contributor_name
      ? cohorts.note(source.cohort_id, source.note_id) : notes.get(source.note_id);
    read.then(n => { if (alive) setNote(n); }).catch(() => { if (alive) setError("This source is no longer available to you. It may have been unshared, deleted, or its membership changed."); });
    return () => { alive = false; };
  }, [source]);
  return <dialog ref={dialog} className="source-dialog" onCancel={onClose} aria-label="Note source">
    <div className="between"><h2>{note?.title ?? source.title}</h2><button className="btn btn-ghost" onClick={onClose} aria-label="Close source">Close</button></div>
    {(note?.contributor_name || source.contributor_name) && <p className="muted">Contributed by {note?.contributor_name || source.contributor_name}</p>}
    {error ? <Alert>{error}</Alert> : !note ? <Spinner /> : <div className="source-body">{note.body}</div>}
    <p className="small faint">Read-only source</p>
  </dialog>;
}

export function DisplayNameForm() {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { api.get<{display_name?: string}>("/auth/me").then(u => setName(u.display_name ?? "")).catch(() => {}); }, []);
  return <form className="cohort-card stack" onSubmit={async e => {
    e.preventDefault(); setBusy(true); setMessage(""); setError("");
    try { await cohorts.profile(name); setMessage("Display name saved."); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }}>
    <label>Your contributor name<input className="input" required maxLength={255} value={name} onChange={e => setName(e.target.value)} placeholder="Name shown alongside shared notes" /></label>
    <p className="small muted">Required before sharing. Your email is never used for attribution.</p>
    <button className="btn btn-ghost" disabled={busy || !name.trim()}>{busy ? "Saving…" : "Save display name"}</button>
    {message && <p role="status" className="small">{message}</p>}{error && <Alert>{error}</Alert>}
  </form>;
}
