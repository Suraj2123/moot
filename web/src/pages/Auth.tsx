import { useState, type FormEvent } from "react";
import { auth, setToken, ApiError, type User } from "../api";
import { Alert, Spinner } from "../components/ui";

export function AuthPage({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
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
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand">
          <span className="brand-mark">S</span> StudyLink
        </div>

        <div className="card">
          <div className="auth-tabs" role="tablist">
            <button
              role="tab" aria-selected={mode === "login"}
              className={mode === "login" ? "active" : ""}
              onClick={() => { setMode("login"); setError(""); }}
            >Sign in</button>
            <button
              role="tab" aria-selected={mode === "signup"}
              className={mode === "signup" ? "active" : ""}
              onClick={() => { setMode("signup"); setError(""); }}
            >Create account</button>
          </div>

          {error ? <Alert>{error}</Alert> : null}

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email" className="input" type="email" required
                autoComplete="email" autoFocus
                value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="you@school.edu"
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password" className="input" type="password" required
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === "signup" ? "At least 10 characters" : ""}
              />
              {mode === "signup" ? (
                <span className="hint">
                  Length is the only rule — a memorable phrase beats a short scramble.
                </span>
              ) : null}
            </div>

            <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
              {busy ? <Spinner /> : null}
              {mode === "login" ? "Sign in" : "Create account"}
            </button>
          </form>
        </div>

        <p className="small faint" style={{ textAlign: "center", marginTop: 14 }}>
          There is no password reset yet. Use something you will remember.
        </p>
      </div>
    </div>
  );
}
