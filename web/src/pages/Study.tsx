import { useEffect, useState } from "react";
import {
  decks, notes, study, ApiError,
  FORGOT, HARD, GOOD, EASY,
  type Card, type CardPerformance, type Deck, type Note,
  type Progress, type TestQuestion,
} from "../api";
import { Alert, Empty, Skeleton } from "../components/ui";
import { IconPlus } from "../components/Icons";

type View =
  | { name: "decks" }
  | { name: "study"; deckId: number; title: string }
  | { name: "test"; deckId: number; title: string };

export function StudyPage() {
  const [view, setView] = useState<View>({ name: "decks" });

  if (view.name === "study") {
    return (
      <StudySession
        deckId={view.deckId}
        title={view.title}
        onDone={() => setView({ name: "decks" })}
      />
    );
  }
  if (view.name === "test") {
    return (
      <PracticeTest
        deckId={view.deckId}
        title={view.title}
        onDone={() => setView({ name: "decks" })}
      />
    );
  }
  return <DeckList onOpen={setView} />;
}

/* ------------------------------------------------------------------ decks */

function DeckList({ onOpen }: { onOpen: (v: View) => void }) {
  const [items, setItems] = useState<Deck[] | null>(null);
  const [error, setError] = useState("");
  const [making, setMaking] = useState(false);

  async function load() {
    try {
      setItems(await decks.list());
      setError("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load your decks.");
    }
  }

  useEffect(() => { load(); }, []);

  async function remove(deck: Deck) {
    if (!window.confirm(`Delete "${deck.title}" and its ${deck.cards} cards?`)) return;
    const before = items;
    setItems((prev) => prev?.filter((d) => d.id !== deck.id) ?? prev);
    try {
      await decks.remove(deck.id);
    } catch (err) {
      setItems(before);
      setError(err instanceof ApiError ? err.message : "Could not delete that deck.");
    }
  }

  return (
    <div className="content-inner">
      <div className="page-head between">
        <div>
          <h1>Study</h1>
          <p>Flashcards made from your notes, and practice tests built from those cards.</p>
        </div>
        <button className="btn btn-primary" onClick={() => setMaking((v) => !v)}>
          <IconPlus /> Make cards
        </button>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      <ProgressPanel />

      {making ? (
        <DeckMaker onDone={() => { setMaking(false); load(); }} onCancel={() => setMaking(false)} />
      ) : null}

      {items === null ? (
        <Skeleton count={3} />
      ) : items.length === 0 ? (
        <Empty title="No decks yet">
          Pick a note and moot will write flashcards from it — each one quoting the
          sentence it came from, so you can check any card against your own material.
        </Empty>
      ) : (
        <div className="stack">
          {items.map((deck) => (
            <div className="note-item" key={deck.id}>
              <div className="between">
                <div style={{ minWidth: 0 }}>
                  <h3>{deck.title}</h3>
                  <div className="small faint">
                    {deck.cards} cards
                    {deck.due > 0 ? ` · ${deck.due} due` : " · all caught up"}
                  </div>
                </div>
                <div className="row">
                  <button
                    className="btn btn-sm btn-primary"
                    disabled={deck.due === 0}
                    onClick={() => onOpen({ name: "study", deckId: deck.id, title: deck.title })}
                  >
                    {deck.due > 0 ? `Study ${deck.due}` : "Nothing due"}
                  </button>
                  <button
                    className="btn btn-sm"
                    onClick={() => onOpen({ name: "test", deckId: deck.id, title: deck.title })}
                  >
                    Test
                  </button>
                  <button className="btn btn-ghost btn-sm danger" onClick={() => remove(deck)}>
                    Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DeckMaker({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [available, setAvailable] = useState<Note[] | null>(null);
  const [noteId, setNoteId] = useState<number | null>(null);
  const [count, setCount] = useState(10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ cards: number; rejected: number } | null>(null);

  useEffect(() => {
    notes.list()
      .then((list) => { setAvailable(list); setNoteId(list[0]?.id ?? null); })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load notes."));
  }, []);

  async function make() {
    if (noteId == null) return;
    setBusy(true);
    setError("");
    try {
      const made = await decks.create(noteId, count);
      setResult(made);
      setTimeout(onDone, 1200);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not make cards from that note.");
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      {error ? <Alert>{error}</Alert> : null}
      {result ? (
        <Alert kind="info">
          {result.cards} cards made
          {result.rejected > 0
            ? ` · ${result.rejected} dropped for not quoting the note`
            : ""}
        </Alert>
      ) : null}

      <div className="field">
        <label htmlFor="deck-note">Note</label>
        <select
          id="deck-note" className="select"
          value={noteId ?? ""} disabled={!available?.length}
          onChange={(e) => setNoteId(Number(e.target.value))}
        >
          {(available ?? []).map((note) => (
            <option key={note.id} value={note.id}>{note.title}</option>
          ))}
        </select>
        {available?.length === 0 ? (
          <div className="small faint">Add a note first — there is nothing to make cards from.</div>
        ) : null}
      </div>

      <div className="field">
        <label htmlFor="deck-count">How many cards</label>
        <input
          id="deck-count" className="input" type="number" min={1} max={20}
          value={count}
          onChange={(e) => setCount(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
        />
      </div>

      <div className="row">
        <button className="btn btn-primary" onClick={make} disabled={busy || noteId == null}>
          {busy ? "Writing cards…" : "Make cards"}
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
        <span className="small faint">
          Every card must quote your note. Ones that don't are dropped.
        </span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ study */

const GRADES = [
  { grade: FORGOT, label: "Forgot", hint: "again tomorrow" },
  { grade: HARD, label: "Hard", hint: "sooner" },
  { grade: GOOD, label: "Good", hint: "on schedule" },
  { grade: EASY, label: "Easy", hint: "later" },
];

function StudySession({
  deckId, title, onDone,
}: { deckId: number; title: string; onDone: () => void }) {
  const [queue, setQueue] = useState<Card[] | null>(null);
  const [at, setAt] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(0);

  useEffect(() => {
    decks.study(deckId)
      .then(setQueue)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not start studying."));
  }, [deckId]);

  const card = queue?.[at];

  async function grade(value: number) {
    if (!card) return;
    // Advance immediately. The scheduling call is not something the student
    // waits on -- they have already decided, and a spinner between cards is
    // the fastest way to make a study session feel like paperwork.
    setAt((i) => i + 1);
    setFlipped(false);
    setDone((n) => n + 1);
    try {
      await decks.review(card.id, value);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That grade did not save.");
    }
  }

  // Space flips, 1-4 grade. A study session is dozens of interactions; making
  // each one a mouse trip is how a tool stops getting used.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!card) return;
      if (event.code === "Space") {
        event.preventDefault();
        setFlipped((v) => !v);
        return;
      }
      if (!flipped) return;
      const index = ["Digit1", "Digit2", "Digit3", "Digit4"].indexOf(event.code);
      if (index >= 0) grade(GRADES[index].grade);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [card, flipped]);

  if (error && !queue) return <div className="content-inner"><Alert>{error}</Alert></div>;
  if (queue === null) return <div className="content-inner"><Skeleton count={2} height={120} /></div>;

  if (!card) {
    return (
      <div className="content-inner">
        <div className="page-head"><h1>{title}</h1></div>
        <Empty title={done > 0 ? `${done} reviewed` : "Nothing due"}>
          {done > 0
            ? "That is this deck done for now. Cards come back on their own schedule."
            : "Every card in this deck is scheduled for later."}
        </Empty>
        <button className="btn" onClick={onDone}>Back to decks</button>
      </div>
    );
  }

  return (
    <div className="content-inner">
      <div className="page-head between">
        <div>
          <h1>{title}</h1>
          <p>{queue.length - at} left · space to flip</p>
        </div>
        <button className="btn btn-ghost" onClick={onDone}>Done</button>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      <div className="flashcard" onClick={() => setFlipped((v) => !v)}>
        <div className="flashcard-face">{card.front}</div>
        {flipped ? (
          <>
            <div className="flashcard-divider" />
            <div className="flashcard-back">{card.back}</div>
            {card.evidence ? (
              <div className="snippet" style={{ marginTop: 14 }}>
                <span className="small faint">From your note: </span>{card.evidence}
              </div>
            ) : null}
          </>
        ) : (
          <div className="small faint" style={{ marginTop: 18 }}>Click, or press space</div>
        )}
      </div>

      {flipped ? (
        <div className="row" style={{ marginTop: 14 }}>
          {GRADES.map((g, i) => (
            <button key={g.grade} className="btn" onClick={() => grade(g.grade)}>
              {g.label} <span className="faint small">{i + 1} · {g.hint}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------- test */

function PracticeTest({
  deckId, title, onDone,
}: { deckId: number; title: string; onDone: () => void }) {
  const [kind, setKind] = useState<"multiple_choice" | "written">("multiple_choice");
  const [questions, setQuestions] = useState<TestQuestion[] | null>(null);
  const [at, setAt] = useState(0);
  const [answer, setAnswer] = useState("");
  const [verdict, setVerdict] = useState<string | null>(null);
  const [score, setScore] = useState({ right: 0, close: 0, wrong: 0 });
  const [error, setError] = useState("");

  function start(nextKind: "multiple_choice" | "written") {
    setKind(nextKind);
    setQuestions(null);
    setAt(0);
    setVerdict(null);
    setAnswer("");
    setScore({ right: 0, close: 0, wrong: 0 });
    decks.test(deckId, nextKind)
      .then((body) => setQuestions(body.questions))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not build a test."));
  }

  useEffect(() => { start("multiple_choice"); }, [deckId]);

  const question = questions?.[at];

  async function submit(given: string) {
    if (!question) return;
    let result: string;
    if (question.kind === "multiple_choice") {
      result = given === question.answer ? "correct" : "wrong";
    } else {
      try {
        result = (await decks.check(given, question.answer)).verdict;
      } catch {
        result = given.trim() === question.answer.trim() ? "correct" : "wrong";
      }
    }
    setVerdict(result);
    setScore((s) => ({
      right: s.right + (result === "correct" ? 1 : 0),
      close: s.close + (result === "close" ? 1 : 0),
      wrong: s.wrong + (result === "wrong" ? 1 : 0),
    }));
  }

  function next() {
    setAt((i) => i + 1);
    setVerdict(null);
    setAnswer("");
  }

  if (error) return <div className="content-inner"><Alert>{error}</Alert></div>;
  if (questions === null) return <div className="content-inner"><Skeleton count={3} /></div>;

  if (!question) {
    const total = score.right + score.close + score.wrong;
    return (
      <div className="content-inner">
        <div className="page-head"><h1>{title} — results</h1></div>
        <div className="card">
          <h2>{score.right} / {total}</h2>
          <p className="faint">
            {score.close > 0 ? `${score.close} close. ` : ""}
            {score.wrong > 0 ? `${score.wrong} wrong.` : "Nothing wrong."}
          </p>
          <div className="row">
            <button className="btn btn-primary" onClick={() => start(kind)}>Again</button>
            <button className="btn btn-ghost" onClick={onDone}>Back to decks</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="content-inner">
      <div className="page-head between">
        <div>
          <h1>{title}</h1>
          <p>Question {at + 1} of {questions.length}</p>
        </div>
        <div className="row">
          <button
            className={`btn btn-sm${kind === "multiple_choice" ? " btn-primary" : ""}`}
            onClick={() => start("multiple_choice")}
          >
            Choices
          </button>
          <button
            className={`btn btn-sm${kind === "written" ? " btn-primary" : ""}`}
            onClick={() => start("written")}
          >
            Written
          </button>
          <button className="btn btn-ghost btn-sm" onClick={onDone}>Done</button>
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginBottom: 14 }}>{question.prompt}</h3>

        {question.kind === "multiple_choice" ? (
          <div className="stack">
            {question.choices.map((choice) => {
              const chosen = verdict !== null && choice === answer;
              const isAnswer = verdict !== null && choice === question.answer;
              return (
                <button
                  key={choice}
                  className={`choice${isAnswer ? " right" : chosen ? " wrong" : ""}`}
                  disabled={verdict !== null}
                  onClick={() => { setAnswer(choice); submit(choice); }}
                >
                  {choice}
                </button>
              );
            })}
          </div>
        ) : (
          <div className="field">
            <input
              className="input" autoFocus placeholder="Type your answer"
              value={answer} disabled={verdict !== null}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !verdict) submit(answer); }}
            />
            {verdict === null ? (
              <button className="btn btn-primary" style={{ marginTop: 10 }} onClick={() => submit(answer)}>
                Check
              </button>
            ) : null}
          </div>
        )}

        {verdict !== null ? (
          <div style={{ marginTop: 14 }}>
            <Alert kind={verdict === "correct" ? "info" : undefined}>
              {verdict === "correct" ? "Correct."
                : verdict === "close" ? `Close. The answer was: ${question.answer}`
                : `The answer was: ${question.answer}`}
            </Alert>
            <button className="btn btn-primary" onClick={next}>
              {at + 1 === questions.length ? "See results" : "Next"}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}


/* --------------------------------------------------------------- progress */

function pct(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

/**
 * The numbers a student reads before deciding what to do, and the cards that
 * decision should be about.
 *
 * Deliberately above the deck list rather than on its own page: "what am I bad
 * at" is the question worth answering, and a tab nobody opens does not answer
 * it.
 */
function ProgressPanel() {
  const [summary, setSummary] = useState<Progress | null>(null);
  const [weak, setWeak] = useState<CardPerformance[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    study.progress().then(setSummary).catch(() => setSummary(null));
    study.weak(undefined, 8).then(setWeak).catch(() => setWeak([]));
  }, []);

  if (!summary || summary.cards === 0) return null;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="stat-row">
        <Stat label="Mastered" value={`${summary.mastered}`} sub={`of ${summary.cards}`} />
        <Stat label="Learning" value={`${summary.learning}`} />
        <Stat label="Not started" value={`${summary.new}`} />
        <Stat label="Accuracy" value={pct(summary.accuracy)} sub={`${summary.attempts} answers`} />
        <Stat
          label="Streak"
          value={`${summary.streak_days}`}
          sub={summary.streak_days === 1 ? "day" : "days"}
        />
      </div>

      {summary.cards > 0 ? (
        <div className="mastery-bar" title={`${summary.mastered} mastered, ${summary.learning} learning, ${summary.new} not started`}>
          <div className="seg mastered" style={{ width: `${(summary.mastered / summary.cards) * 100}%` }} />
          <div className="seg learning" style={{ width: `${(summary.learning / summary.cards) * 100}%` }} />
          <div className="seg new" style={{ width: `${(summary.new / summary.cards) * 100}%` }} />
        </div>
      ) : null}

      {weak.length > 0 ? (
        <>
          <button
            className="btn btn-ghost btn-sm"
            style={{ marginTop: 12, paddingLeft: 0 }}
            onClick={() => setOpen((v) => !v)}
          >
            {weak.length} card{weak.length === 1 ? "" : "s"} need more practice {open ? "▴" : "▾"}
          </button>
          {open ? (
            <div className="stack" style={{ marginTop: 8 }}>
              {weak.map((card) => (
                <div className="upload-row" key={card.id}>
                  <div style={{ minWidth: 0 }}>
                    <div className="upload-name">{card.front}</div>
                    <div className="small faint">{card.back}</div>
                  </div>
                  <span className="badge" title={`${card.correct} of ${card.attempts} correct`}>
                    {card.correct}/{card.attempts}
                    {card.lapses > 0 ? ` · ${card.lapses} forgotten` : ""}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="small faint">{label}{sub ? ` · ${sub}` : ""}</div>
    </div>
  );
}
