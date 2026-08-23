import { useEffect, useRef, useState } from "react";
import { sharing, ApiError, type PublicDeck } from "../api";
import { Alert, Empty, Skeleton } from "../components/ui";
import { IconSearch } from "../components/Icons";

/**
 * Public decks other people published.
 *
 * Semantic rather than keyword search, because the index it runs on already
 * exists -- the same embeddings that match notes to assignments -- and because
 * someone looking for a deck rarely knows what its author called it. "the
 * powerhouse of the cell" should find a deck titled "Bio 101 midterm".
 *
 * An empty query lists rather than returning nothing: a discovery page that
 * shows a blank slate until you guess a search term teaches you it is empty.
 */
export function DiscoverPage() {
  const [query, setQuery] = useState("");
  const [decks, setDecks] = useState<PublicDeck[] | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<string | null>(null);
  const latest = useRef(0);

  useEffect(() => {
    const run = ++latest.current;
    const timer = setTimeout(() => {
      sharing.discover(query)
        .then((found) => {
          // Out-of-order responses would otherwise let a slow early query
          // overwrite the results of a later one.
          if (run === latest.current) { setDecks(found); setError(""); }
        })
        .catch((err) => {
          if (run !== latest.current) return;
          setError(err instanceof ApiError ? err.message : "Could not search decks.");
        });
    }, query ? 300 : 0);
    return () => clearTimeout(timer);
  }, [query]);

  async function fork(deck: PublicDeck) {
    setError("");
    try {
      await sharing.fork(deck.slug);
      setCopied(deck.slug);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not copy that deck.");
    }
  }

  return (
    <div className="content-inner">
      <div className="page-head">
        <h1>Discover</h1>
        <p>
          Decks other people published. Copy one and it becomes yours — private,
          unstudied, and yours to edit.
        </p>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      <div className="search-row">
        <IconSearch />
        <input
          className="input"
          value={query}
          placeholder="What are you studying? Try a topic, not a title."
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {decks === null ? (
        <Skeleton count={3} />
      ) : decks.length === 0 ? (
        <Empty title={query ? "Nothing matched" : "No public decks yet"}>
          {query
            ? "No published deck covers that. Try broader words, or write the cards yourself — a note with :: in it becomes a deck."
            : "Nobody has published a deck yet. Publish one of yours from the Study page and it will show up here."}
        </Empty>
      ) : (
        <div className="stack">
          {decks.map((deck) => (
            <div className="note-item" key={deck.slug}>
              <div className="between">
                <div style={{ minWidth: 0 }}>
                  <h3>{deck.title}</h3>
                  <div className="small faint">
                    {deck.cards} card{deck.cards === 1 ? "" : "s"} · {deck.owner}
                  </div>
                </div>
                <div className="row">
                  <a className="btn btn-sm" href={`/d/${deck.slug}`}>Look inside</a>
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={() => fork(deck)}
                    disabled={copied === deck.slug}
                  >
                    {copied === deck.slug ? "Copied" : "Copy"}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
