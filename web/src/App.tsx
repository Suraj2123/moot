import { useEffect, useState } from "react";
import { auth, setUnauthorizedHandler, getToken, setToken, type User } from "./api";
import { AuthPage } from "./pages/Auth";
import { ChatPage } from "./pages/Chat";
import { NotesPage } from "./pages/Notes";
import { StudyPage } from "./pages/Study";
import { AssignmentsPage } from "./pages/Assignments";
import { SettingsPage } from "./pages/Settings";
import { SharedDeckPage } from "./pages/SharedDeck";
import { DiscoverPage } from "./pages/Discover";
import {
  IconAssignments, IconCards, IconChat, IconMenu, IconMoon,
  IconNotes, IconSearch, IconSettings, IconSun,
} from "./components/Icons";
import { Spinner } from "./components/ui";

type Tab = "chat" | "notes" | "study" | "discover" | "assignments" | "settings";

/**
 * The one URL-driven route in an otherwise state-driven app.
 *
 * A share link has to be a real URL -- that is what makes it shareable -- so
 * `/d/{slug}` is read from the address bar rather than from component state.
 * Everything else stays tab state, because no other screen in this app is
 * worth linking to from outside it.
 */
function sharedSlug(): string | null {
  const match = window.location.pathname.match(/^\/d\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

// Order is the product's opinion about what this app is for. Notes and the
// cards they declare come first; Ask and Assignments are the two features that
// need an API key or a Canvas connection, so they sit below the ones that
// always work.
const TABS: { id: Tab; label: string; icon: () => JSX.Element; title: string }[] = [
  { id: "study", label: "Study", icon: IconCards, title: "Study" },
  { id: "notes", label: "Notes", icon: IconNotes, title: "Notes" },
  { id: "discover", label: "Discover", icon: IconSearch, title: "Public decks" },
  { id: "chat", label: "Ask", icon: IconChat, title: "Ask your notes" },
  { id: "assignments", label: "Match", icon: IconAssignments, title: "Match notes to what you are studying for" },
  { id: "settings", label: "Settings", icon: IconSettings, title: "Settings" },
];

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [tab, setTab] = useState<Tab>("study");
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

  const active = TABS.find((t) => t.id === tab)!;

  return (
    <div className="shell">
      {menuOpen ? <div className="scrim" onClick={() => setMenuOpen(false)} /> : null}

      <nav className={`sidebar${menuOpen ? " open" : ""}`}>
        <div className="brand">
          <span className="brand-mark">m</span> moot
        </div>

        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={`nav-item${tab === id ? " active" : ""}`}
            onClick={() => { setTab(id); setMenuOpen(false); }}
          >
            <Icon /> {label}
          </button>
        ))}

        <div className="nav-spacer" />

        <button
          className="nav-item"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? <IconSun /> : <IconMoon />}
          {theme === "dark" ? "Light mode" : "Dark mode"}
        </button>
        <div className="small faint" style={{ padding: "6px 10px" }}>
          {user.email}
        </div>
      </nav>

      <main className="main">
        <header className="topbar">
          <button className="btn btn-ghost btn-sm menu-btn" onClick={() => setMenuOpen(true)} aria-label="Menu">
            <IconMenu />
          </button>
          <h1>{active.title}</h1>
          <div className="spacer" />
        </header>

        {/* Chat owns its own scrolling so the composer can stay pinned. */}
        {tab === "chat" ? (
          <ChatPage />
        ) : (
          <div className="content">
            {tab === "notes" ? <NotesPage /> : null}
            {tab === "study" ? <StudyPage /> : null}
            {tab === "discover" ? <DiscoverPage /> : null}
            {tab === "assignments" ? <AssignmentsPage /> : null}
            {tab === "settings" ? (
              <SettingsPage
                theme={theme}
                onThemeChange={setTheme}
                onSignedOut={() => setUser(null)}
              />
            ) : null}
          </div>
        )}
      </main>
    </div>
  );
}
