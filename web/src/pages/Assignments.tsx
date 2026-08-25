import { useEffect, useState } from "react";
import { assignments, targets, ApiError, type Assignment, type Match } from "../api";
import { Alert, Empty, Skeleton, ConfidenceBadge, ScoreBar } from "../components/ui";

export function AssignmentsPage() {
  const [items, setItems] = useState<Assignment[] | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    assignments.list()
      .then(setItems)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load assignments."));
  }, []);

  return (
    <div className="content-inner">
      <div className="page-head">
        <h1>Match</h1>
        <p>
          Name something you are studying for and see which of your notes bear
          on it, with the sentence that made each match.
        </p>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      <TargetComposer onCreated={(t) => { setItems((prev) => [t, ...(prev ?? [])]); setOpen(t.id); }} />

      {items === null ? (
        <Skeleton count={4} />
      ) : items.length === 0 ? (
        <Empty title="Nothing to match against yet">
          Type an exam topic, a syllabus section, or a question above — moot
          will show which of your notes cover it. Connecting Canvas in Settings
          fills this in from your assignments as well.
        </Empty>
      ) : (
        <div className="stack">
          {items.map((a) => (
            <div className="match" key={a.id}>
              <div className="match-head" onClick={() => setOpen(open === a.id ? null : a.id)}>
                <div style={{ minWidth: 0 }}>
                  <strong style={{ fontSize: 14 }}>{a.name}</strong>
                  <div className="small faint">
                    {a.source === "canvas" ? a.course || "Canvas" : "Yours"}
                    {a.due_at ? ` · due ${new Date(a.due_at).toLocaleDateString()}` : ""}
                    {a.points_possible ? ` · ${a.points_possible} pts` : ""}
                  </div>
                </div>
                <span className="badge">{open === a.id ? "Hide" : "Matching notes"}</span>
              </div>
              {open === a.id ? <MatchList assignmentId={a.id} /> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MatchList({ assignmentId }: { assignmentId: number }) {
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    assignments.matches(assignmentId)
      .then((r) => setMatches(r.matches))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load matches."));
  }, [assignmentId]);

  if (error) return <div className="match-body"><Alert>{error}</Alert></div>;
  if (matches === null) return <div className="match-body" style={{ paddingTop: 12 }}><Skeleton count={2} height={40} /></div>;
  if (matches.length === 0) {
    return (
      <div className="match-body" style={{ paddingTop: 12 }}>
        <p className="small faint" style={{ margin: 0 }}>
          None of your notes matched this one yet.
        </p>
      </div>
    );
  }

  return (
    <div className="match-body" style={{ paddingTop: 12 }}>
      <div className="stack">
        {matches.map((m, i) => (
          <div key={i} style={{ paddingBottom: 10, borderBottom: i < matches.length - 1 ? "1px solid var(--border)" : "none" }}>
            <div className="between">
              <strong style={{ fontSize: 13.5 }}>{m.title}</strong>
              <div className="row">
                <ScoreBar score={m.score} />
                <ConfidenceBadge confidence={m.confidence} />
              </div>
            </div>

            {/* Explainability is the reason to trust a match, so it is shown by
                default rather than hidden behind another click. */}
            {m.evidence?.overlapping_terms?.length ? (
              <div className="concepts">
                {m.evidence.overlapping_terms.slice(0, 8).map((c) => (
                  <span className="badge badge-accent" key={c}>{c}</span>
                ))}
              </div>
            ) : null}

            {m.evidence?.snippet ? <div className="snippet">{m.evidence.snippet}</div> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Name something to study for.
 *
 * This is the form that makes the matching engine reachable. Before it, the
 * only way to get a row to match against was a Canvas sync, which needs a
 * personal access token that plenty of universities disable for students --
 * so the part of moot with the evidence layer and the measured retrieval
 * behind it did nothing at all for them.
 *
 * The description field is not optional decoration. A name alone is a few
 * words; a pasted syllabus section or exam brief is the richest signal anyone
 * will ever hand this feature, and the retriever is much better with it.
 */
function TargetComposer({ onCreated }: { onCreated: (t: Assignment) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(0);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setError("");
    try {
      const made = await targets.create(name.trim(), description.trim());
      setPending(made.notes_pending ?? 0);
      setName("");
      setDescription("");
      setOpen(false);
      onCreated(made);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not make that target.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <>
        {pending > 0 ? (
          <Alert kind="info">
            {pending} note{pending === 1 ? " is" : "s are"} still being indexed, so
            matches may be incomplete for a moment.
          </Alert>
        ) : null}
        <button className="btn btn-primary" style={{ marginBottom: 16 }} onClick={() => setOpen(true)}>
          + What are you studying for?
        </button>
      </>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      {error ? <Alert>{error}</Alert> : null}
      <div className="field">
        <label htmlFor="target-name">Topic, exam, or question</label>
        <input
          id="target-name" className="input" value={name} autoFocus
          placeholder="Midterm 2: optimisation and regularisation"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") create(); }}
        />
      </div>
      <div className="field">
        <label htmlFor="target-detail">Detail (optional, but it matches much better)</label>
        <textarea
          id="target-detail" className="textarea" style={{ minHeight: 90 }}
          value={description}
          placeholder="Paste the syllabus section, the exam brief, or the question you are answering."
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      <div className="row">
        <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim()}>
          {busy ? "Matching…" : "Match my notes"}
        </button>
        <button className="btn btn-ghost" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
