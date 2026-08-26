import { useEffect, useState } from "react";
import { decks, ApiError, type Deck } from "../api";
import { Alert, Empty, Skeleton } from "../components/ui";
import { PracticeTest } from "./Study";

/**
 * Pick a deck, sit a test.
 *
 * A separate entry from Flashcards because the two are different intentions.
 * Review is maintenance -- what is due today. A practice test is a rehearsal
 * for an exam, taken over a whole deck regardless of schedule, and burying it
 * behind a per-deck button meant nobody found it when that was the thing they
 * actually wanted.
 *
 * Decks with fewer than four cards are still offered, because `build_test`
 * falls back to written answers rather than refusing: three cards cannot make
 * a four-option question, and padding with nonsense would make the test
 * easier rather than harder.
 */
export function TestMenuPage() {
  const [items, setItems] = useState<Deck[] | null>(null);
  const [error, setError] = useState("");
  const [taking, setTaking] = useState<Deck | null>(null);

  useEffect(() => {
    decks.list()
      .then(setItems)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load your decks."),
      );
  }, []);

  if (taking) {
    return (
      <PracticeTest
        deckId={taking.id}
        title={taking.title}
        onDone={() => setTaking(null)}
      />
    );
  }

  return (
    <div className="content-inner">
      <div className="page-head">
        <h1>Practice test</h1>
        <p>
          A test over a whole deck, not just what is due. Wrong options are
          other answers from the same deck, so they are genuinely confusable.
        </p>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {items === null ? (
        <Skeleton count={3} />
      ) : items.length === 0 ? (
        <Empty title="No decks to test on yet">
          Make a deck first — type one on the Flashcards page, or write
          <code> term :: definition</code> in a note.
        </Empty>
      ) : (
        <div className="stack">
          {items.map((deck) => (
            <div className="note-item" key={deck.id}>
              <div className="between">
                <div style={{ minWidth: 0 }}>
                  <h3>{deck.title}</h3>
                  <div className="small faint">
                    {deck.cards} card{deck.cards === 1 ? "" : "s"}
                    {deck.cards < 4 ? " · written answers, too few for choices" : ""}
                  </div>
                </div>
                <button
                  className="btn btn-sm btn-primary"
                  disabled={deck.cards === 0}
                  onClick={() => setTaking(deck)}
                >
                  {deck.cards === 0 ? "Empty" : "Start test"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
