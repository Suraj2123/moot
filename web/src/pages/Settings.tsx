import { useEffect, useState } from "react";
import {
  canvas, usage, jobs, reindex, auth, setToken,
  ApiError, type CanvasStatus, type Usage, type Job,
} from "../api";
import { Alert, JobBadge, Ago, Spinner } from "../components/ui";

interface SettingsProps {
  theme: string;
  onThemeChange: (theme: string) => void;
  onSignedOut: () => void;
}

export function SettingsPage({ theme, onThemeChange, onSignedOut }: SettingsProps) {
  return (
    <div className="content-inner">
      <div className="page-head">
        <h1>Settings</h1>
        <p>Canvas, background work, and what this account has spent on AI.</p>
      </div>
      <CanvasCard />
      <UsageCard />
      <JobsCard />
      <AccountCard theme={theme} onThemeChange={onThemeChange} onSignedOut={onSignedOut} />
    </div>
  );
}

function CanvasCard() {
  const [status, setStatus] = useState<CanvasStatus | null>(null);
  const [url, setUrl] = useState("");
  const [token, setTokenValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = () => canvas.status().then(setStatus).catch(() => setStatus({ connected: false }));
  useEffect(() => { load(); }, []);

  async function connect() {
    setBusy(true); setError(""); setNotice("");
    try {
      await canvas.connect(url.trim(), token.trim());
      // Never keep the token in component state after it is sent.
      setTokenValue("");
      setNotice("Canvas connected. Run a sync to pull your courses.");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not connect Canvas.");
    } finally { setBusy(false); }
  }

  async function sync() {
    setBusy(true); setError(""); setNotice("");
    try {
      await canvas.sync();
      setNotice("Sync queued. It runs in the background — watch it below.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start a sync.");
    } finally { setBusy(false); }
  }

  async function disconnect() {
    setBusy(true);
    try { await canvas.disconnect(); await load(); } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <div className="between" style={{ marginBottom: 12 }}>
        <h2>Canvas</h2>
        {status?.connected ? (
          <span className={`badge ${status.verified_at ? "badge-good" : "badge-warn"}`}>
            {status.verified_at ? "connected" : "not yet verified"}
          </span>
        ) : <span className="badge">not connected</span>}
      </div>

      {error ? <Alert>{error}</Alert> : null}
      {notice ? <Alert kind="info">{notice}</Alert> : null}

      {status?.connected ? (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>
            <span className="mono">{status.api_url}</span>
            {status.verified_at
              ? <> · last worked <Ago iso={status.verified_at} /></>
              : " · not used successfully yet"}
          </p>
          <div className="row">
            <button className="btn btn-primary" onClick={sync} disabled={busy}>
              {busy ? <Spinner /> : null} Sync now
            </button>
            <button className="btn btn-danger" onClick={disconnect} disabled={busy}>Disconnect</button>
          </div>
        </>
      ) : (
        <>
          <div className="field">
            <label htmlFor="canvas-url">Canvas URL</label>
            <input
              id="canvas-url" className="input" value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://canvas.yourschool.edu"
            />
          </div>
          <div className="field">
            <label htmlFor="canvas-token">Access token</label>
            <input
              id="canvas-token" className="input" type="password" value={token}
              onChange={(e) => setTokenValue(e.target.value)}
              placeholder="Canvas → Account → Settings → New Access Token"
            />
            <span className="hint">
              Stored encrypted and never shown again. It is only ever sent to your Canvas.
            </span>
          </div>
          <button className="btn btn-primary" onClick={connect} disabled={busy || !url.trim() || !token.trim()}>
            {busy ? <Spinner /> : null} Connect Canvas
          </button>
        </>
      )}
    </div>
  );
}

function UsageCard() {
  const [data, setData] = useState<Usage | null>(null);
  useEffect(() => { usage.summary().then(setData).catch(() => {}); }, []);
  if (!data) return null;

  const pct = data.budget_usd
    ? Math.min(100, (data.cost_usd / data.budget_usd) * 100)
    : 0;

  return (
    <div className="card">
      <h2 style={{ marginBottom: 10 }}>AI usage</h2>
      <div className="between small muted" style={{ marginBottom: 6 }}>
        <span>${data.cost_usd.toFixed(4)} used{data.budget_usd ? ` of $${data.budget_usd.toFixed(2)}` : ""}</span>
        <span>{data.calls} call{data.calls === 1 ? "" : "s"}</span>
      </div>
      {data.budget_usd ? (
        <div className="score-bar" style={{ width: "100%", height: 6 }}>
          <div style={{ width: `${pct}%`, background: pct > 85 ? "var(--warn)" : "var(--accent)" }} />
        </div>
      ) : null}
      <p className="small faint" style={{ marginBottom: 0, marginTop: 10 }}>
        Rolling 30 days. Questions your notes cannot answer are refused before any
        model call, so they cost nothing.
      </p>
    </div>
  );
}

function JobsCard() {
  const [items, setItems] = useState<Job[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => jobs.list().then(setItems).catch(() => setItems([]));
  useEffect(() => {
    load();
    // Poll while anything is in flight, so a running sync visibly finishes.
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="card">
      <div className="between" style={{ marginBottom: 12 }}>
        <h2>Background work</h2>
        <button
          className="btn btn-sm"
          disabled={busy}
          onClick={async () => { setBusy(true); try { await reindex(); await load(); } finally { setBusy(false); } }}
        >
          Reindex notes
        </button>
      </div>

      {items === null ? null : items.length === 0 ? (
        <p className="small faint" style={{ margin: 0 }}>Nothing has run yet.</p>
      ) : (
        <div className="stack">
          {items.slice(0, 6).map((job) => (
            <div className="between small" key={job.id}>
              <div style={{ minWidth: 0 }}>
                <span className="mono">{job.kind}</span>{" "}
                <Ago iso={job.finished_at ?? job.created_at} />
                {job.detail ? <div className="faint" style={{ fontSize: 12 }}>{job.detail}</div> : null}
              </div>
              <JobBadge status={job.status} />
            </div>
          ))}
        </div>
      )}
      <p className="small faint" style={{ marginBottom: 0, marginTop: 12 }}>
        Jobs need a worker running: <span className="mono">python scripts/run_worker.py</span>
      </p>
    </div>
  );
}

function AccountCard({ theme, onThemeChange, onSignedOut }: SettingsProps) {
  // Theme is owned by App -- a second copy here was how the sidebar label went
  // stale after changing it from this page.
  return (
    <div className="card">
      <h2 style={{ marginBottom: 12 }}>Account</h2>
      <div className="field">
        <label>Appearance</label>
        <div className="row">
          {["dark", "light"].map((t) => (
            <button
              key={t}
              className={`btn btn-sm${theme === t ? " btn-primary" : ""}`}
              onClick={() => onThemeChange(t)}
            >
              {t}
            </button>
          ))}
        </div>
      </div>
      <button
        className="btn btn-danger"
        onClick={async () => { try { await auth.logout(); } finally { setToken(null); onSignedOut(); } }}
      >
        Sign out
      </button>
    </div>
  );
}
