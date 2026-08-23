import { useEffect, useState } from "react";
import { sharing, getToken, ApiError, type SharedDeck } from "../api";
import { Alert, Skeleton } from "../components/ui";

/**
 * A deck someone sent you a link to.
 *
 * The only page in the app that renders without a session, which is the whole
 * point of it: a share link that opens a sign-in wall is not a share link. So
 * this deliberately shows the cards first and asks for an account second --
 * whoever opened it can read the entire deck, and only copying it into their
 * own account needs a login.
 *
 * Cards are shown as question and answer together rather than as a review
 * session. A stranger's deck is something you skim to decide whether it is
 * any good, not something you study one card at a time before you own it.
 */
export function SharedDeckPage({ slug }: { slug: string }) {
  const [deck, setDeck] = useState<SharedDeck | null>(null);
  const [error, setError] = useState("");
  const [forking, setForking] = useState(false);
  const [forked, setForked] = useState(false);

  useEffect(() => {
    sharing.read(slug)
      .then(setDeck)
      .catch((err) =>
        setError(
          err instanceof ApiError && err.status === 404
            ? "There is no deck at this link. It may have been unshared, or the link may be mistyped."
            : "Could not load that deck.",
        ),
      );
  }, [slug]);

  async function fork() {
    setForking(true);
    setError("");
    try {
      await sharing.fork(slug);
      setForked(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not copy that deck.");
      setForking(false);
    }
  }

  const signedIn = Boolean(getToken());

  return (
    <div className="shared-page">
      <header className="shared-head">
        <a className="brand" href="/app/">
          <span className="brand-mark">m</span> moot
        </a>
        {signedIn ? (
          <a className="btn btn-sm" href="/app/">Your decks</a>
        ) : (
          <a className="btn btn-sm btn-primary" href="/app/">Sign in</a>
        )}
      </header>

      <div className="content-inner">
        {error ? <Alert>{error}</Alert> : null}

        {deck === null && !error ? (
          <Skeleton count={4} />
        ) : deck ? (
          <>
            <div className="page-head between">
              <div style={{ minWidth: 0 }}>
                <h1>{deck.title}</h1>
                <p>
                  {deck.cards.length} card{deck.cards.length === 1 ? "" : "s"} · shared by{" "}
                  {deck.owner}
                </p>
              </div>
              {forked ? (
                <a className="btn btn-primary" href="/app/">Open your copy</a>
              ) : signedIn ? (
                <button className="btn btn-primary" onClick={fork} disabled={forking}>
                  {forking ? "Copying…" : "Copy to my decks"}
                </button>
              ) : (
                <a className="btn btn-primary" href="/app/">
                  Sign up to study this
                </a>
              )}
            </div>

            {forked ? (
              <Alert kind="info">
                Copied. It is yours now — private, unstudied, and yours to edit.
              </Alert>
            ) : null}

            <div className="stack">
              {deck.cards.map((card) => (
                <div className="shared-card" key={card.id}>
                  <div className="shared-front">{card.front}</div>
                  <div className="shared-back">{card.back}</div>
                </div>
              ))}
            </div>

            <p className="small faint" style={{ marginTop: 28 }}>
              moot turns your notes into flashcards. Write{" "}
              <code>term :: definition</code> in a note and it becomes a card —
              free, no API key.
            </p>
          </>
        ) : null}
      </div>
    </div>
  );
}
