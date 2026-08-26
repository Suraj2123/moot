import { Wordmark } from "../components/Wordmark";

/**
 * A page for something that does not exist yet.
 *
 * The rule it follows: describe the idea, and be unambiguous that none of it
 * is built. A "coming soon" screen that reads like a feature tour is how a
 * product loses the benefit of the doubt on everything else it claims -- so
 * the tense stays future throughout and there is nothing here to press.
 *
 * Deck sharing already works and is a genuine part of this, so the page says
 * where that is rather than implying the whole idea is unreachable.
 */
export function CommunityPage({ onDiscover }: { onDiscover: () => void }) {
  return (
    <div className="content-inner">
      <div className="page-head">
        <span className="badge">Coming soon</span>
        <h1 style={{ marginTop: 12 }}>moot community</h1>
        <p>
          A place to publish what you made for a topic — notes, quizzes, and
          flashcards — so that anyone else studying it can use them instead of
          building the same thing again from an empty page.
        </p>
      </div>

      <div className="card">
        <h2>What it will be</h2>
        <ul className="pricing-list" style={{ marginTop: 10 }}>
          <li>
            Publish a whole topic, not just a deck: the notes behind the cards,
            the practice tests, and the questions that turned out to matter.
          </li>
          <li>
            Search by what you are studying rather than by what somebody called
            their file — the same semantic search that already matches your
            notes to a topic.
          </li>
          <li>
            Copy anything into your own account and edit it, with your own
            review history, because someone else's schedule says nothing about
            what you know.
          </li>
          <li>Free, like the rest of moot. No paywall on anyone's material.</li>
        </ul>
      </div>

      <div className="card">
        <h2>What works today</h2>
        <p className="small faint" style={{ margin: "6px 0 14px" }}>
          Publishing and copying single decks is already here — community is
          about widening that to everything around a deck.
        </p>
        <button className="btn" onClick={onDiscover}>Find a deck</button>
      </div>

      <div className="community-foot">
        <Wordmark size={34} />
      </div>
    </div>
  );
}
