import { useEffect, useState } from "react";
import { cohorts, type Cohort, type CohortMember, type SharedNote, type Source } from "../api";
import { Alert, Empty, Skeleton } from "../components/ui";
import { DisplayNameForm, SourceDialog } from "../components/CohortControls";

export function CohortsPage({ invite = "", onAsk }: { invite?: string; onAsk: (cohort: number) => void }) {
  const [items, setItems] = useState<Cohort[] | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [term, setTerm] = useState("");
  const [code, setCode] = useState(invite);
  const [preview, setPreview] = useState<Pick<Cohort, "id" | "name" | "term"> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() { try { setItems(await cohorts.list()); } catch (e) { setError((e as Error).message); } }
  useEffect(() => { load(); }, []);
  async function act(work: () => Promise<void>) {
    setBusy(true); setError(""); try { await work(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="content-inner">
    <div className="page-head"><h1>Learn together, share deliberately.</h1><p>Pool notes with your class. Your library stays private until you choose a note to share.</p></div>
    {error && <Alert>{error}</Alert>}
    <div className="cohort-layout">
      <aside className="stack">
        <h2>Your cohorts</h2>
        {items === null ? <Skeleton count={2} /> : items.length === 0 ? <p className="muted">No cohorts yet. Join a class or create one below.</p> : items.map(c => <button key={c.id} className={`cohort-card cohort-nav ${selected === c.id ? "selected" : ""}`} onClick={() => setSelected(c.id)}><strong>{c.name}</strong><span className="small muted">{c.term} · {c.role === "instructor" ? "Administrator" : "Member"}</span></button>)}
        <form className="cohort-card stack" onSubmit={e => { e.preventDefault(); act(async () => { const c = await cohorts.create(name, term); await load(); setSelected(c.id); setName(""); setTerm(""); }); }}>
          <h3>Create a cohort</h3>
          <label>Class or section<input className="input" value={name} required maxLength={120} placeholder="CS 101 · Section 2" onChange={e => setName(e.target.value)} /></label>
          <label>Term<input className="input" value={term} required maxLength={80} placeholder="Fall 2026" onChange={e => setTerm(e.target.value)} /></label>
          <button className="btn btn-primary" disabled={busy || !name.trim() || !term.trim()}>Create cohort</button>
        </form>
        <form className="cohort-card stack" onSubmit={e => { e.preventDefault(); act(async () => { setPreview(await cohorts.preview(code.trim())); }); }}>
          <h3>Join your class</h3>
          <label>Invite code<input className="input" value={code} required maxLength={64} placeholder="Paste your class invite code" onChange={e => { setCode(e.target.value); setPreview(null); }} /></label>
          <button className="btn btn-ghost" disabled={busy || !code.trim()}>Check invitation</button>
          {preview && <div className="stack"><strong>{preview.name} · {preview.term}</strong><p className="small muted">Joining does not share any of your notes.</p><button type="button" className="btn btn-primary" disabled={busy} onClick={() => act(async () => {
            const c = await cohorts.join(code.trim()); await load(); setSelected(c.id); setPreview(null); setCode("");
            window.history.replaceState({}, "", window.location.pathname);
          })}>Confirm and join</button></div>}
        </form>
        <DisplayNameForm />
      </aside>
      <section>{selected ? <CohortDetail key={selected} id={selected} onAsk={onAsk} onChanged={load} onLeft={() => { setSelected(null); load(); }} /> : <Empty title="A shared space for one class"><p>Select a cohort to browse notes and members. Share your own notes from Notes and folders.</p></Empty>}</section>
    </div>
  </div>;
}

function CohortDetail({ id, onAsk, onChanged, onLeft }: { id: number; onAsk: (id: number) => void; onChanged: () => void; onLeft: () => void }) {
  const [cohort, setCohort] = useState<Cohort | null>(null);
  const [members, setMembers] = useState<CohortMember[]>([]);
  const [notes, setNotes] = useState<SharedNote[]>([]);
  const [source, setSource] = useState<Source | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Source[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function load() {
    const [c, m, n] = await Promise.all([cohorts.get(id), cohorts.members(id), cohorts.notes(id)]);
    setCohort(c); setMembers(m); setNotes(n);
  }
  useEffect(() => { load().catch(e => setError(e.message)); }, [id]);
  async function act(work: () => Promise<unknown>, refresh = true) {
    setBusy(true); setError(""); setMessage("");
    try { await work(); if (refresh) { await load(); onChanged(); setResults(null); } } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  if (!cohort) return error ? <Alert>{error}</Alert> : <Skeleton />;
  const admin = cohort.role === "instructor";
  return <div className="stack">
    <div className="cohort-card">
      <div className="between"><div><span className="badge">{cohort.term}</span><h2>{cohort.name}</h2></div><button className="btn btn-primary" onClick={() => onAsk(id)}>Ask with this cohort</button></div>
      <p className="muted">{notes.length} shared {notes.length === 1 ? "note" : "notes"} · {members.length} {members.length === 1 ? "member" : "members"}</p>
      <p className="small muted">Read classmates’ notes and see who contributed. Share your own from Notes and folders.</p>
    </div>
    {error && <Alert>{error}</Alert>}{message && <p role="status">{message}</p>}
    <div className="between"><h3>Shared notes</h3><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act(async () => {})}>Refresh</button></div>
    <form className="row" onSubmit={e => { e.preventDefault(); act(async () => { setResults(await cohorts.search(query, id)); }, false); }}>
      <input className="input" aria-label="Search this cohort and my notes" value={query} onChange={e => { setQuery(e.target.value); setResults(null); }} placeholder="Search this cohort + my notes" />
      <button className="btn btn-ghost" disabled={busy || !query.trim()}>Search</button>
    </form>
    {results !== null ? <div className="stack"><p className="small muted">Matches from your notes and this cohort</p>{!results.length && <Empty title="No matching indexed notes" />}{results.map(s => <button className="note-item cohort-nav" key={s.note_id} onClick={() => setSource(s)}><strong>{s.title}</strong><span className="small muted">{s.contributor_name ? `Contributed by ${s.contributor_name}` : "Your note"}</span></button>)}</div> : !notes.length ? <Empty title="The first note starts the conversation">Open Notes and folders and choose a note to share. Joining alone leaves every note private.</Empty> : notes.map(n => <div className="cohort-card between" key={n.id}>
      <button className="btn btn-ghost cohort-nav" onClick={() => setSource({note_id:n.id,title:n.title,cohort_id:id,contributor_name:n.contributor_name})}><strong>{n.title}</strong><span className="small muted">{n.contributor_name ? `Contributed by ${n.contributor_name}` : "Your note"}{n.index_status === "pending" ? " · Indexing pending" : ""}</span></button>
      {admin && <button className="btn btn-ghost btn-sm danger" disabled={busy} onClick={() => { if (window.confirm(`Remove “${n.title}” from the shared pool? The owner's private note stays intact.`)) act(() => cohorts.moderate(id,n.id)); }}>Remove from pool</button>}
    </div>)}
    {notes.some(n => n.index_status === "pending") && <p className="small muted">Pending notes are readable now. They become searchable when their owner’s indexing job finishes. Refresh to check.</p>}
    <div className="cohort-card stack"><h3>Members</h3>{members.map(m => <div className="between" key={m.user_id}><span>{m.display_name || "Member without a display name"}<span className="small faint"> · {m.role === "instructor" ? "Administrator" : "Member"}</span></span>{admin && m.role !== "instructor" && <div className="row"><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => { if (window.confirm(`Transfer administration to ${m.display_name || "this member"}? You will become a member.`)) act(() => cohorts.transfer(id,m.user_id)); }}>Make administrator</button><button className="btn btn-ghost btn-sm danger" disabled={busy} onClick={() => { if (window.confirm("Remove this member and their shared notes? This member will not be able to rejoin with an invite.")) act(() => cohorts.removeMember(id,m.user_id)); }}>Remove member</button></div>}</div>)}</div>
    {admin && <div className="cohort-card stack"><h3>Invite classmates</h3><p className="small muted">Send this code or link to your class. Each person confirms before joining.</p><input className="input mono" aria-label="Cohort invite code" readOnly value={cohort.invite_code ?? ""} /><div className="row"><button className="btn btn-ghost" disabled={busy} onClick={() => act(async () => { await navigator.clipboard.writeText(`${window.location.origin}/app/?cohort_invite=${cohort.invite_code}`); setMessage("Invite link copied."); }, false)}>Copy invite link</button><button className="btn btn-ghost" disabled={busy} onClick={() => { if (window.confirm("Replace the invite code? Existing invitation links will stop working.")) act(() => cohorts.rotate(id)); }}>Rotate invite</button></div></div>}
    {admin ? <p className="small muted">Transfer administration to another member before leaving. Administrator is an app permission, not a verified instructor identity.</p> : <button className="btn btn-ghost danger" disabled={busy} onClick={() => { if (window.confirm("Leave this cohort? Your shared notes will become private, and future access to classmates’ sources will end. Previously delivered answers remain.")) act(async () => { await cohorts.leave(id); onLeft(); }, false); }}>Leave cohort</button>}
    {source && <SourceDialog source={source} onClose={() => setSource(null)} />}
  </div>;
}
