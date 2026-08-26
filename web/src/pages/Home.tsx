import { useEffect, useState } from "react";
import { decks, folders, notes, ApiError } from "../api";
import { Wordmark } from "../components/Wordmark";
import {
  IconAssignments, IconCards, IconChat, IconNotes, IconSearch, IconUpload,
} from "../components/Icons";

/**
 * The central menu.
 *
 * Replaces the sidebar as the way into everything. A permanent nav rail is
 * right for an app someone lives inside all day; this is one people open with
 * a specific job -- revise these cards, file these notes, ask about this
 * topic -- and a hub asks what the job is instead of showing six answers at
 * once and making them read all six.
 *
 * The tiles carry live counts. "Flashcards" is a label; "Flashcards · 3 decks,
 * 12 due" is a reason to press it, and it is the same query the page behind it
 * runs anyway.
 *
 * Mooty sits at the bottom on purpose. It is the answer to "I do not know
 * where to start", which is a thing you conclude *after* reading the tiles and
 * finding that none of them is obviously the thing you wanted.
 */

export type Destination =
  | "study" | "notes" | "test" | "chat" | "match" | "discover" | "community";

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
    id: "chat",
    title: "Ask Mooty",
    blurb: "Questions answered from your own notes, with citations.",
    icon: IconChat,
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
      "Coming soon. A place to publish notes, quizzes, and flashcards on any topic, so anyone studying it can use them instead of starting over.",
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

      {/* The bottom of the page, because "I don't know where to start" is what
          you conclude after reading the tiles rather than before. */}
      <form
        className="mooty"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) onAsk(question.trim());
        }}
      >
        <span className="mooty-mark">m</span>
        <input
          className="input mooty-input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Not sure where to start? Ask Mooty — “what should I revise for Friday?”"
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
  );
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
