import { useEffect, useState } from "react";
import { assignments, ApiError, type Assignment, type Match } from "../api";
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
        <h1>Assignments</h1>
        <p>Pulled from Canvas. Open one to see which of your notes bear on it, and why.</p>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {items === null ? (
        <Skeleton count={4} />
      ) : items.length === 0 ? (
        <Empty title="No assignments yet">
          Connect Canvas in Settings and run a sync — assignments and their descriptions
          come across automatically.
        </Empty>
      ) : (
        <div className="stack">
          {items.map((a) => (
            <div className="match" key={a.id}>
              <div className="match-head" onClick={() => setOpen(open === a.id ? null : a.id)}>
                <div style={{ minWidth: 0 }}>
                  <strong style={{ fontSize: 14 }}>{a.name}</strong>
                  <div className="small faint">
                    {a.course}
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
