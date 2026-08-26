import { useEffect, useState } from "react";
import { auth, setUnauthorizedHandler, getToken, setToken, type User } from "./api";
import { AuthPage } from "./pages/Auth";
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
import { Wordmark } from "./components/Wordmark";
import { IconMoon, IconSettings, IconSun } from "./components/Icons";
import { Spinner } from "./components/ui";

type Screen = "home" | Destination | "settings";

const TITLES: Record<Screen, string> = {
  home: "moot",
  study: "Flashcards",
  notes: "Notes",
  test: "Practice test",
  match: "Match",
  chat: "Ask Mooty",
  discover: "Find a deck",
  community: "moot community",
  settings: "Settings",
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
  const [screen, setScreen] = useState<Screen>("home");
  // A question typed into Mooty on the hub, handed to the chat to send itself.
  const [asked, setAsked] = useState("");
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
    setUnauthorizedHandler(() => setUser(null));
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

  if (!user) return <AuthPage onSignedIn={setUser} />;

  function go(to: Destination) {
    setAsked("");
    setScreen(to);
  }

  return (
    <div className="shell">
      {/* Icon-only, and as narrow as a touch target allows. Everything you
          navigate to lives on the hub; what is left here is the two things
          that are never the reason you opened the app -- settings, and the
          account they belong to. */}
      <nav className="rail">
        <button
          className="rail-home"
          onClick={() => go("home" as Destination)}
          title="moot — home"
          aria-label="Home"
        >
          m
        </button>

        <div className="rail-spacer" />

        <button
          className={`rail-btn${screen === "settings" ? " active" : ""}`}
          onClick={() => setScreen("settings")}
          title="Settings and account"
          aria-label="Settings and account"
        >
          <IconSettings />
        </button>
        <button
          className="rail-btn"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          title={theme === "dark" ? "Light mode" : "Dark mode"}
          aria-label={theme === "dark" ? "Light mode" : "Dark mode"}
        >
          {theme === "dark" ? <IconSun /> : <IconMoon />}
        </button>
      </nav>

      <main className="main">
        <header className="topbar">
          {screen === "home" ? (
            <Wordmark size={19} />
          ) : (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setScreen("home")}>
                ← Menu
              </button>
              <h1>{TITLES[screen]}</h1>
            </>
          )}
          <div className="spacer" />
          <span className="small faint topbar-user">{user.email}</span>
        </header>

        {screen === "chat" ? (
          <ChatPage initialQuestion={asked} />
        ) : (
          <div className="content">
            {screen === "home" ? (
              <HomePage
                onGo={go}
                onAsk={(question) => { setAsked(question); setScreen("chat"); }}
              />
            ) : null}
            {screen === "study" ? <StudyPage /> : null}
            {screen === "notes" ? <NotesPage /> : null}
            {screen === "test" ? <TestMenuPage /> : null}
            {screen === "match" ? <AssignmentsPage /> : null}
            {screen === "discover" ? <DiscoverPage /> : null}
            {screen === "community" ? (
              <CommunityPage onDiscover={() => setScreen("discover")} />
            ) : null}
            {screen === "settings" ? (
              <SettingsPage
                theme={theme}
                onThemeChange={setTheme}
                onSignedOut={() => { setUser(null); setScreen("home"); }}
              />
            ) : null}
          </div>
        )}
      </main>
    </div>
  );
}
