import { useEffect, useState, type ReactNode } from "react";

export function Spinner() {
  return <span className="spinner" aria-label="Loading" />;
}

export function Empty({
  title, children, action,
}: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

export function Alert({ kind = "error", children }: { kind?: "error" | "info" | "warn"; children: ReactNode }) {
  return <div className={`alert alert-${kind}`} role={kind === "error" ? "alert" : undefined}>{children}</div>;
}

export function Skeleton({ height = 58, count = 3 }: { height?: number; count?: number }) {
  return (
    <div className="stack" aria-busy="true" aria-label="Loading">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height }} />
      ))}
    </div>
  );
}

/**
 * Confidence arrives as a calibrated 0-1 float, not a label.
 *
 * Showing the raw number alone invites false precision -- 0.611 reads as a
 * measurement when it is a heuristic. The word carries the meaning and the
 * number stays available for anyone who wants it, which is also how the
 * explainability elsewhere in the app is framed.
 */
export function ConfidenceBadge({ confidence }: { confidence: number }) {
  const label = confidence >= 0.6 ? "strong" : confidence >= 0.35 ? "possible" : "weak";
  const kind =
    confidence >= 0.6 ? "badge-good" : confidence >= 0.35 ? "badge-warn" : "badge";
  return (
    <span className={`badge ${kind}`} title={`confidence ${confidence.toFixed(3)}`}>
      {label}
    </span>
  );
}

export function ScoreBar({ score }: { score: number }) {
  return (
    <div className="score-bar" title={`score ${score.toFixed(3)}`}>
      <div style={{ width: `${Math.max(2, Math.min(100, score * 100))}%` }} />
    </div>
  );
}

/** Relative time, because "3 minutes ago" is what a person wants from a job list. */
export function Ago({ iso }: { iso: string | null }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  if (!iso) return <span className="faint">—</span>;
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const label =
    seconds < 60 ? "just now"
    : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago`
    : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago`
    : `${Math.floor(seconds / 86400)}d ago`;
  return <span className="faint" title={new Date(iso).toLocaleString()}>{label}</span>;
}

export function JobBadge({ status }: { status: string }) {
  const kind =
    status === "succeeded" ? "badge-good"
    : status === "failed" ? "badge-bad"
    : status === "running" ? "badge-accent"
    : "badge";
  return (
    <span className={`badge ${kind}`}>
      {status === "running" ? <span className="dots"><span /><span /><span /></span> : null}
      {status}
    </span>
  );
}
