import { useEffect, useState } from "react";
import { auth, setUnauthorizedHandler, getToken, setToken, type User } from "./api";
import { AuthPage } from "./pages/Auth";
import { LandingPage } from "./pages/Landing";
import { CohortsPage } from "./pages/Cohorts";
import { ChatPage } from "./pages/Chat";
import { NotesPage } from "./pages/Notes";
import { StudyPage } from "./pages/Study";
import { TestMenuPage } from "./pages/TestMenu";
import { AssignmentsPage } from "./pages/Assignments";
import { SettingsPage } from "./pages/Settings";
import { SharedDeckPage } from "./pages/SharedDeck";
import { DiscoverPage } from "./pages/Discover";
import { CommunityPage } from "./pages/Community";
import { HomePage, type Destination } from "./pages/Home";

import { IconMenu, IconSettings, IconCohorts, IconNotes, IconCards, IconAssignments, IconSearch, IconChat } from "./components/Icons";
import { Spinner } from "./components/ui";

type Screen = "home" | Destination | "settings";

const TITLES: Record<Screen, string> = {
  home: "Overview",
  study: "Flashcards",
  notes: "Notes",
  test: "Practice test",
  match: "Match",
  chat: "Ask Moot",
  discover: "Find a deck",
  community: "moot community",
  settings: "Settings",
  cohorts: "Class cohorts",
};

/**
 * The one URL-driven route in an otherwise state-driven app.
 *
 * A share link has to be a real URL -- that is what makes it shareable -- so
 * `/d/{slug}` is read from the address bar. Everything else is screen state,
 * because no other view here is worth linking to from outside the app.
 */
function sharedSlug(): string | null {
  const match = window.location.pathname.match(/^\/d\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [authMode, setAuthMode] = useState<"login" | "signup" | null>(null);
  const invite = new URLSearchParams(window.location.search).get("cohort_invite") ?? "";
  const [screen, setScreen] = useState<Screen>(invite ? "cohorts" : "home");
  const [chatCohort, setChatCohort] = useState<number | null>(null);
  // A question typed into Mooty on the hub, handed to the chat to send itself.
  const [asked, setAsked] = useState("");
  const [selectedNote, setSelectedNote] = useState<number | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [theme, setTheme] = useState(
    () => localStorage.getItem("studylink.theme") ?? "dark"
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("studylink.theme", theme);
  }, [theme]);

  // A stored token may have expired while the tab was closed, so it is
  // validated once on boot rather than trusted. One place, one decision.
  useEffect(() => {
    setUnauthorizedHandler(() => resetSession(false));
    if (!getToken()) { setChecking(false); return; }
    auth.me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setChecking(false));
  }, []);

  // Before the session check, not after: a shared deck is readable by someone
  // with no account at all, and making them wait on an auth round-trip to see
  // a public page is the wall this feature exists to remove.
  const slug = sharedSlug();
  if (slug) return <SharedDeckPage slug={slug} />;

  if (checking) {
    return (
      <div style={{ height: "100%", display: "grid", placeItems: "center" }}>
        <Spinner />
      </div>
    );
  }

  if (!user) {
    if (invite || authMode) {
      return <AuthPage initialMode={authMode ?? "login"} onSignedIn={setUser}
        onBack={invite ? undefined : () => setAuthMode(null)} />;
    }
    return <LandingPage onAuthenticate={setAuthMode} />;
  }

  function resetSession(returnToLanding = true) {
    setToken(null);
    setUser(null);
    setSelectedNote(null);
    setAsked("");
    setChatCohort(null);
    if (returnToLanding) setAuthMode(null);
    setScreen(invite ? "cohorts" : "home");
    setMenuOpen(false);
  }

  function go(to: Screen) {
    setMenuOpen(false);
    setSelectedNote(null);
    setAsked("");
    setChatCohort(null);
    setScreen(to);
  }

  return (
    <div className="shell workspace">
      <aside className={`workspace-sidebar${menuOpen ? " is-open" : ""}`} id="workspace-navigation">
        <button className="workspace-brand" onClick={() => go("home")} aria-label="Moot home">
          <svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M16 7v18M7 11l18 10M7 21l18-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/><path d="m12 3 4 4 4-4M12 29l4-4 4 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          moot<span>.</span>
        </button>
        <nav aria-label="Workspace navigation" className="workspace-links">
          <p className="workspace-label">My workspace</p>
          {([
            ["home", "Overview", IconNotes],
            ["notes", "My notes", IconNotes],
            ["chat", "Ask Moot", IconChat],
          ] as const).map(([to, label, Icon]) => <button key={to} className={`workspace-link${screen === to ? " active" : ""}`} aria-current={screen === to ? "page" : undefined} onClick={() => go(to)}><Icon />{label}</button>)}
          <p className="workspace-label">Study space</p>
          {([
            ["study", "Flashcards", IconCards],
            ["test", "Practice tests", IconAssignments],
            ["match", "Match my notes", IconSearch],
            ["discover", "Find a deck", IconSearch],
            ["cohorts", "Class cohorts", IconCohorts],
            ["community", "Community", IconChat],
          ] as const).map(([to, label, Icon]) => <button key={to} className={`workspace-link${screen === to ? " active" : ""}`} aria-current={screen === to ? "page" : undefined} onClick={() => go(to)}><Icon />{label}</button>)}
        </nav>
        <div className="workspace-account">
          <button className={`workspace-link${screen === "settings" ? " active" : ""}`} onClick={() => go("settings")} aria-current={screen === "settings" ? "page" : undefined}><IconSettings />Settings & account</button>
          <span className="workspace-email" title={user.email ?? undefined}>{user.email ?? "Your account"}</span>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <button className="btn btn-ghost workspace-menu" aria-expanded={menuOpen} aria-controls="workspace-navigation" aria-label="Toggle navigation" onClick={() => setMenuOpen(!menuOpen)}><IconMenu /></button>
          <span className="workspace-breadcrumb">My workspace <span>/</span> {TITLES[screen]}</span>
          <div className="spacer" />
          <button className="btn btn-ghost btn-sm" onClick={() => go("settings")}><IconSettings /><span>Settings</span></button>
        </header>

        {screen === "chat" ? (
          <ChatPage initialQuestion={asked} initialCohort={chatCohort} />
        ) : (
          <div className={`content${screen === "home" ? " content-home" : ""}`}>
            {screen === "home" ? (
              <HomePage
                onGo={go}
                onOpenNote={(id) => { go("notes"); setSelectedNote(id); }}
                onAsk={(question) => { setAsked(question); setChatCohort(null); setScreen("chat"); }}
              />
            ) : null}
            {screen === "cohorts" ? <CohortsPage invite={invite} onAsk={id => { setAsked(""); setChatCohort(id); setScreen("chat"); }} /> : null}
            {screen === "study" ? <StudyPage /> : null}
            {screen === "notes" ? <NotesPage initialNoteId={selectedNote} /> : null}
            {screen === "test" ? <TestMenuPage /> : null}
            {screen === "match" ? <AssignmentsPage /> : null}
            {screen === "discover" ? <DiscoverPage /> : null}
            {screen === "community" ? (
              <CommunityPage onDiscover={() => setScreen("discover")} />
            ) : null}
            {screen === "settings" ? (
              <SettingsPage
                email={user.email ?? "No email on this account"}
                theme={theme}
                onThemeChange={setTheme}
                onSignedOut={resetSession}
              />
            ) : null}
          </div>
        )}
      </main>
    </div>
  );
}
