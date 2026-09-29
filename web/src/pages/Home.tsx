import { useEffect, useState } from "react";
import { decks, folders, notes, ApiError } from "../api";
import { Wordmark } from "../components/Wordmark";
import {
  IconAssignments, IconCards, IconChat, IconNotes, IconSearch, IconUpload,
} from "../components/Icons";

/** Personal study tools, with Mooty always available below the grid. */
export type Destination =
  | "study" | "notes" | "test" | "chat" | "match" | "discover" | "community" | "cohorts";

interface Tile {
  id: Destination;
  title: string;
  blurb: string;
  icon: () => JSX.Element;
  soon?: boolean;
}

const TILES: Tile[] = [
  {
    id: "study",
    title: "Flashcards",
    blurb: "Review what is due, or write a new set.",
    icon: IconCards,
  },
  {
    id: "notes",
    title: "Notes and folders",
    blurb: "Write, upload, and file. Cards you mark with :: appear in Flashcards.",
    icon: IconNotes,
  },
  {
    id: "test",
    title: "Practice test",
    blurb: "Sit a test built from a deck you already have.",
    icon: IconAssignments,
  },
  {
    id: "match",
    title: "Match my notes",
    blurb: "Name a topic or exam and see which notes cover it.",
    icon: IconSearch,
  },
  {
    id: "discover",
    title: "Find a deck",
    blurb: "Search decks other people published, and copy one.",
    icon: IconUpload,
  },
  {
    id: "community",
    title: "moot community",
    blurb:
      "Share notes, quizzes, and flashcards with fellow learners.",
    icon: IconChat,
    soon: true,
  },
];

export function HomePage({
  onGo,
  onAsk,
}: {
  onGo: (to: Destination) => void;
  onAsk: (question: string) => void;
}) {
  const [counts, setCounts] = useState<Record<string, string>>({});
  const [question, setQuestion] = useState("");

  useEffect(() => {
    Promise.all([
      decks.list().catch(() => []),
      notes.list().catch(() => []),
      folders.list().catch(() => []),
    ])
      .then(([deckList, noteList, folderList]) => {
        const due = deckList.reduce((sum, d) => sum + d.due, 0);
        setCounts({
          study: deckList.length
            ? `${plural(deckList.length, "deck")}${due ? ` · ${due} due` : " · all caught up"}`
            : "Nothing yet",
          test: deckList.length ? plural(deckList.length, "deck") + " to test on" : "Nothing yet",
          notes: noteList.length
            ? `${plural(noteList.length, "note")}${folderList.length ? ` · ${plural(folderList.length, "folder")}` : ""}`
            : "Nothing yet",
        });
      })
      .catch((err) => {
        if (!(err instanceof ApiError)) throw err;
      });
  }, []);

  return (
    <div className="hub">
      <div className="hub-head">
        <Wordmark size={54} />
        <p>What are you working on?</p>
      </div>

      <div className="tiles">
        {TILES.map((tile) => (
          <button
            key={tile.id}
            className={`tile${tile.soon ? " tile-soon" : ""}`}
            onClick={() => onGo(tile.id)}
          >
            <span className="tile-icon"><tile.icon /></span>
            <span className="tile-body">
              <span className="tile-title">
                {tile.title}
                {tile.soon ? <span className="badge tile-badge">Coming soon</span> : null}
              </span>
              <span className="tile-blurb">{tile.blurb}</span>
            </span>
            {counts[tile.id] ? <span className="tile-count">{counts[tile.id]}</span> : null}
          </button>
        ))}
      </div>

      <div className="mooty-dock">
        <label className="mooty-label" htmlFor="home-mooty">Ask Mooty</label>
        <form
          className="mooty"
          onSubmit={(e) => {
            e.preventDefault();
            if (question.trim()) onAsk(question.trim());
          }}
        >
          <span className="mooty-mark">m</span>
          <input
            id="home-mooty"
            className="input mooty-input"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="What should I revise? Ask about your notes…"
            aria-label="Ask Mooty"
          />
          <button className="btn btn-primary btn-sm" type="submit" disabled={!question.trim()}>
            Ask
          </button>
        </form>
        <p className="mooty-note small faint">
          Mooty answers from your own notes and says so when they do not cover
          something, rather than guessing.
        </p>
      </div>
    </div>
  );
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
