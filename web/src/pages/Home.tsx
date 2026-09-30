import { useEffect, useState } from "react";
import { decks, folders, notes, type Deck, type Note, type Folder } from "../api";
import { IconAssignments, IconCards, IconChat, IconNotes, IconSearch } from "../components/Icons";
import { Alert, Spinner } from "../components/ui";

export type Destination =
  | "study" | "notes" | "test" | "chat" | "match" | "discover" | "community" | "cohorts";

export function HomePage({ onGo, onAsk, onOpenNote }: {
  onGo: (to: Destination) => void;
  onAsk: (question: string) => void;
  onOpenNote: (id: number) => void;
}) {
  const [data, setData] = useState<{ notes: Note[]; decks: Deck[]; folders: Folder[] } | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [tab, setTab] = useState<"notes" | "decks">("notes");
  const [question, setQuestion] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError(false);
    setData(null);
    Promise.all([notes.list(), decks.list(), folders.list()])
      .then(([noteList, deckList, folderList]) => {
        if (!cancelled) setData({ notes: noteList, decks: deckList, folders: folderList });
      })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [attempt]);

  const due = data?.decks.reduce((sum, deck) => sum + deck.due, 0) ?? 0;
  return (
    <div className="overview-layout">
      <section className="overview-main" aria-labelledby="overview-title">
        <div className="overview-heading">
          <p className="workspace-kicker">Let's make it click</p>
          <h1 id="overview-title">A world worth<br /><em>understanding.</em></h1>
          <p>Big ideas. Small steps. You've got this.</p>
          <span className="overview-orbit" aria-hidden="true">✳</span>
        </div>
        <div className="overview-tabs" aria-label="Your study material">
          <button aria-pressed={tab === "notes"} onClick={() => setTab("notes")}>Your notes {data && <span>{data.notes.length}</span>}</button>
          <button aria-pressed={tab === "decks"} onClick={() => setTab("decks")}>Flashcards {data && <span>{data.decks.length}</span>}</button>
          <span className="overview-private">Private by default</span>
        </div>
        {error ? <Alert>We couldn't load your workspace. <button className="btn btn-sm" onClick={() => setAttempt(attempt + 1)}>Try again</button></Alert> : !data ? <div className="overview-empty" role="status"><Spinner /> Loading your workspace…</div> : tab === "notes" ? (
          data.notes.length ? <div className="overview-note-grid">
            {data.notes.slice(0, 4).map((note, index) => <button className={`overview-note${index === 0 ? " featured" : ""}`} key={note.id} onClick={() => onOpenNote(note.id)}>
              <span className="overview-note-icon"><IconNotes /></span>
              <span className="workspace-kicker">{note.course || note.folder || data.folders.find(folder => folder.id === note.folder_id)?.name || "Your notes"}</span>
              <h2>{note.title}</h2>
              <span className="overview-note-meta">{note.chars.toLocaleString()} characters · {note.source_type}</span>
              <span className="overview-note-bottom"><span>{note.visibility === "cohort" ? "Shared with cohort" : "Private note"}</span><span aria-hidden="true">↗</span></span>
            </button>)}
          </div> : <div className="overview-empty"><IconNotes /><h2>A place for your next idea.</h2><p>Bring a lecture, a chapter, or a question.<br />Your notes stay private unless you choose to share them.</p><button className="btn btn-primary" onClick={() => onGo("notes")}>Add your first note ↗</button></div>
        ) : data.decks.length ? <div className="overview-note-grid">
          {data.decks.slice(0, 4).map(deck => <article className="overview-note" key={deck.id}><span className="overview-note-icon"><IconCards /></span><span className="workspace-kicker">{deck.visibility} deck</span><h2>{deck.title}</h2><span className="overview-note-meta">{deck.cards} cards · {deck.due} due</span><button className="btn btn-sm" onClick={() => onGo("study")}>Go to flashcards →</button></article>)}
        </div> : <div className="overview-empty"><IconCards /><h2>Small cards. Lasting knowledge.</h2><p>Create a deck and build your understanding, one question at a time.</p><button className="btn btn-primary" onClick={() => onGo("study")}>Create a deck ↗</button></div>}
        {data && <div className="overview-materials"><span>{data.folders.length} folders · {data.notes.length} notes · {data.decks.length} decks</span><button className="btn btn-ghost btn-sm" onClick={() => onGo(tab === "notes" ? "notes" : "study")}>View all {tab === "notes" ? "notes" : "flashcards"} →</button></div>}
        <div className="overview-study-strip"><IconCards /><div><strong>A little practice goes a long way.</strong><p>{data ? due ? `${due} cards are ready for another look.` : "Bring your notes to life with flashcards." : "Build understanding at your own pace."}</p></div><button className="btn btn-sm" onClick={() => onGo("study")}>Study →</button></div>
        <p className="workspace-kicker overview-tools-label">Make room for your next aha</p>
        <div className="overview-tools">
          {([
            ["test", "Practice tests", "Find what clicks.", IconAssignments],
            ["match", "Match my notes", "Connect your ideas.", IconSearch],
            ["discover", "Find a deck", "A fresh perspective.", IconCards],
          ] as const).map(([to, label, detail, Icon]) => <button key={to} onClick={() => onGo(to)}><Icon /><strong>{label}</strong><span>{detail}</span></button>)}
        </div>
      </section>
      <aside className="overview-ask" aria-labelledby="ask-moot-title">
        <div className="overview-ask-title"><IconChat /><h2 id="ask-moot-title">Ask Moot</h2><span aria-hidden="true">✧</span></div>
        <p className="overview-grounded"><span />Grounded in your notes</p>
        <div className="overview-ask-intro"><span className="overview-spark" aria-hidden="true">✳</span><h3>A little help.<br /><em>A bigger aha.</em></h3><p>Bring a tricky idea. Ask Moot answers from your notes, with sources you can come back to.</p></div>
        <p className="workspace-kicker">Start with a question</p>
        <div className="overview-prompts">{["Help me understand a topic from my notes", "What do my notes say about…"].map(prompt => <button key={prompt} onClick={() => setQuestion(prompt)}>{prompt} <span aria-hidden="true">↗</span></button>)}</div>
        <form className="overview-composer" onSubmit={event => { event.preventDefault(); if (question.trim()) onAsk(question.trim()); }}>
          <label className="workspace-kicker" htmlFor="overview-question">Your question</label>
          <textarea id="overview-question" value={question} onChange={event => setQuestion(event.target.value)} placeholder="What would you like to understand?" rows={3} />
          <button className="btn btn-primary btn-sm" type="submit" disabled={!question.trim()}>Ask Moot ↗</button>
        </form>
        <p className="overview-ask-footnote">Your material. A clearer explanation.</p>
      </aside>
    </div>
  );
}
