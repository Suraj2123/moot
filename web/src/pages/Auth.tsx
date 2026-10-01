import { useState, type FormEvent } from "react";
import { auth, setToken, ApiError, type User } from "../api";
import { Alert, Spinner } from "../components/ui";
import "./auth.css";

export function AuthPage({ onSignedIn, initialMode = "login", onBack }: {
  onSignedIn: (user: User) => void;
  initialMode?: "login" | "signup";
  onBack?: () => void;
}) {
  const [mode, setMode] = useState<"login" | "signup">(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = mode === "login"
        ? await auth.login(email, password)
        : await auth.signup(email, password);
      setToken(result.token);
      onSignedIn(result.user);
    } catch (err) {
      // The API returns one message for every kind of login failure on purpose
      // (docs/AUTH.md); showing it verbatim keeps that property intact rather
      // than the UI helpfully guessing which half was wrong.
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-welcome">
      <header className="auth-welcome-header">
        <span className="auth-welcome-logo"><svg aria-hidden="true" viewBox="0 0 32 32" fill="none"><path d="M16 7v18M7 11l18 10M7 21l18-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" /><path d="m12 3 4 4 4-4M12 29l4-4 4 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>moot.</span>
        {onBack ? <button className="auth-back" onClick={onBack} disabled={busy}>← Back to Moot</button> : <span className="auth-header-note">Your study space awaits.</span>}
      </header>
      <main className="auth-welcome-main">
        <div className="auth-welcome-heading">
          <p className="auth-eyebrow"><span /> YOUR NOTES. YOUR PACE. YOUR SPACE.</p>
          <h1>{mode === "login" ? <>A little focus.<br /><em>A fresh start.</em></> : <>Room for your ideas.<br /><em>Space to grow.</em></>}</h1>
          <p>{mode === "login" ? "Welcome back. Pick up where your curiosity left off." : "Bring your notes. Find your rhythm. Make it click."}</p>
        </div>
        <section className="auth-welcome-card" aria-label={mode === "login" ? "Sign in to Moot" : "Create your Moot account"}>
          <div className="auth-mode-switch" aria-label="Account access">
            <button type="button" aria-pressed={mode === "login"} disabled={busy}
              onClick={() => { setMode("login"); setError(""); }}>Sign in</button>
            <button type="button" aria-pressed={mode === "signup"} disabled={busy}
              onClick={() => { setMode("signup"); setError(""); }}>Create account</button>
          </div>

          {error ? <Alert>{error}</Alert> : null}

          <form onSubmit={submit} aria-busy={busy}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email" className="input" type="email" required
                autoComplete="email" autoFocus disabled={busy}
                value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="you@school.edu"
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password" className="input" type="password" required disabled={busy}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === "signup" ? "At least 10 characters" : "Your password"}
              />
              {mode === "signup" ? (
                <span className="hint">
                  Length is the only rule — a memorable phrase beats a short scramble.
                </span>
              ) : null}
            </div>

            <button className="btn btn-primary auth-submit" disabled={busy}>
              {busy ? <Spinner /> : null}
              {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account"}
              {!busy && <span aria-hidden="true">↗</span>}
            </button>
          </form>
          <p className="auth-private-note">Private by default. Thoughtfully yours.</p>
        </section>
        <p className="auth-reset-note">Password reset isn't available yet.<br />Use a password you can remember or save in your password manager.</p>
      </main>
      <footer className="auth-welcome-footer">A little less chaos. A little more clarity. <span aria-hidden="true">✧</span></footer>
    </div>
  );
}
