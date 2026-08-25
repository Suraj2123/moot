import { useEffect, useRef, useState } from "react";
import { decks, ApiError, type Card, type Deck } from "../api";
import { Alert } from "../components/ui";

/**
 * Writing a set the way people actually write one: a grid of rows.
 *
 * Two things make this feel like a text editor rather than a form. Tab moves
 * between fields and off the last field into a new row, so a whole set can be
 * typed without touching the mouse. And pasting a block splits it into rows --
 * the fastest route out of Quizlet, Anki, or a spreadsheet is a copy and a
 * paste, and asking someone to retype forty terms to switch tools is asking
 * them not to.
 *
 * Cards a note declares are not editable here. They are re-derived from the
 * note's text on every save, so an edit made here would be reverted with
 * nothing to show for it; the row says where the real text lives instead.
 */

interface Row {
  /** Present once the row exists server-side. */
  id: number | null;
  front: string;
  back: string;
  /** Written by a note's `::` syntax, so not editable here. */
  derived: boolean;
  dirty: boolean;
}

const BLANK: Row = { id: null, front: "", back: "", derived: false, dirty: false };

/**
 * Split pasted text into term/definition pairs.
 *
 * Tab first, because that is what a spreadsheet and Quizlet's own export
 * produce, and a definition containing a comma is far more likely than one
 * containing a tab. Then the app's own `::`, then a comma as the last resort.
 */
export function splitPaste(text: string): { front: string; back: string }[] {
  const rows: { front: string; back: string }[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let parts: string[] | null = null;
    if (line.includes("\t")) parts = line.split("\t");
    else if (line.includes("::")) parts = line.split("::");
    else if (line.includes(",")) {
      const at = line.indexOf(",");
      parts = [line.slice(0, at), line.slice(at + 1)];
    }
    if (!parts || parts.length < 2) continue;
    const front = parts[0].trim();
    const back = parts.slice(1).join(parts[0].includes("\t") ? "\t" : " ").trim();
    if (front && back) rows.push({ front, back });
  }
  return rows;
}

export function DeckEditor({ deckId, onDone }: { deckId: number; onDone: () => void }) {
  const [deck, setDeck] = useState<Deck | null>(null);
  const [title, setTitle] = useState("");
  const [rows, setRows] = useState<Row[]>([{ ...BLANK }]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const focusRow = useRef<number | null>(null);

  useEffect(() => {
    decks.get(deckId)
      .then((detail) => {
        setDeck(detail);
        setTitle(detail.title);
        setRows(
          detail.items.length
            ? detail.items.map(toRow)
            : [{ ...BLANK }],
        );
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not open that deck."),
      );
  }, [deckId]);

  // Focus the row this component just created, once it exists in the DOM.
  useEffect(() => {
    if (focusRow.current === null) return;
    const index = focusRow.current;
    focusRow.current = null;
    document.querySelector<HTMLInputElement>(`#row-${index}-front`)?.focus();
  }, [rows.length]);

  function edit(index: number, field: "front" | "back", value: string) {
    setRows((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [field]: value, dirty: true } : row)),
    );
    setSaved(false);
  }

  function addRow(after?: number) {
    setRows((prev) => {
      const at = after == null ? prev.length : after + 1;
      focusRow.current = at;
      return [...prev.slice(0, at), { ...BLANK }, ...prev.slice(at)];
    });
  }

  async function removeRow(index: number) {
    const row = rows[index];
    if (row.id != null) {
      try {
        await decks.removeCard(row.id);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not delete that card.");
        return;
      }
    }
    setRows((prev) => (prev.length === 1 ? [{ ...BLANK }] : prev.filter((_, i) => i !== index)));
  }

  function onPaste(event: React.ClipboardEvent, index: number) {
    const text = event.clipboardData.getData("text/plain");
    const parsed = splitPaste(text);
    // One row of one field is an ordinary paste; leave it to the browser.
    if (parsed.length < 2 && !text.includes("\n")) return;
    if (!parsed.length) return;

    event.preventDefault();
    setRows((prev) => {
      const next = [...prev];
      const incoming = parsed.map((pair) => ({ ...BLANK, ...pair, dirty: true }));
      // Overwrite the row that was pasted into if it is still empty, so
      // pasting into a fresh editor does not leave a blank first row.
      const start = !prev[index].front && !prev[index].back ? index : index + 1;
      next.splice(start, !prev[index].front && !prev[index].back ? 1 : 0, ...incoming);
      return next;
    });
    setSaved(false);
  }

  function onKeyDown(event: React.KeyboardEvent, index: number, field: "front" | "back") {
    if (event.key === "Enter") {
      event.preventDefault();
      addRow(index);
    }
    if (event.key === "Tab" && !event.shiftKey && field === "back" && index === rows.length - 1) {
      // Tabbing off the last field makes a row rather than leaving the grid,
      // which is what turns this into something you can type continuously.
      event.preventDefault();
      addRow(index);
    }
  }

  async function save() {
    if (!deck) return;
    setSaving(true);
    setError("");
    try {
      const fresh = rows.filter((r) => r.id == null && r.front.trim() && r.back.trim());
      const changed = rows.filter((r) => r.id != null && r.dirty && !r.derived);

      if (title.trim() && title.trim() !== deck.title) {
        await decks.rename(deck.id, title.trim());
      }
      for (const row of changed) {
        await decks.editCard(row.id!, { front: row.front, back: row.back });
      }
      if (fresh.length) {
        await decks.addCards(
          deck.id,
          fresh.map((r) => ({ front: r.front, back: r.back })),
        );
      }

      const detail = await decks.get(deck.id);
      setDeck(detail);
      setRows(detail.items.length ? detail.items.map(toRow) : [{ ...BLANK }]);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save this deck.");
    } finally {
      setSaving(false);
    }
  }

  const pending = rows.filter((r) => r.dirty && r.front.trim() && r.back.trim()).length;

  return (
    <div className="content-inner">
      <div className="page-head between">
        <div style={{ minWidth: 0, flex: 1 }}>
          <input
            className="input title-input"
            value={title}
            placeholder="Untitled deck"
            onChange={(e) => { setTitle(e.target.value); setSaved(false); }}
            aria-label="Deck title"
          />
          <p className="small faint" style={{ marginTop: 6 }}>
            Tab moves across and down · Enter adds a row · paste a block of
            term/definition lines and it splits itself
          </p>
        </div>
        <div className="row">
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : saved && !pending ? "Saved" : `Save${pending ? ` ${pending}` : ""}`}
          </button>
          <button className="btn btn-ghost" onClick={onDone}>Done</button>
        </div>
      </div>

      {error ? <Alert>{error}</Alert> : null}

      <div className="stack">
        {rows.map((row, index) => (
          <div className="card-row" key={row.id ?? `new-${index}`}>
            <span className="card-row-num">{index + 1}</span>
            <input
              id={`row-${index}-front`}
              className="input"
              value={row.front}
              placeholder="Term"
              disabled={row.derived}
              onChange={(e) => edit(index, "front", e.target.value)}
              onKeyDown={(e) => onKeyDown(e, index, "front")}
              onPaste={(e) => onPaste(e, index)}
            />
            <input
              id={`row-${index}-back`}
              className="input"
              value={row.back}
              placeholder="Definition"
              disabled={row.derived}
              onChange={(e) => edit(index, "back", e.target.value)}
              onKeyDown={(e) => onKeyDown(e, index, "back")}
              onPaste={(e) => onPaste(e, index)}
            />
            {row.derived ? (
              <span className="small faint card-row-note" title="Written by a note">
                from note
              </span>
            ) : (
              <button
                className="btn btn-ghost btn-sm danger"
                onClick={() => removeRow(index)}
                aria-label={`Remove row ${index + 1}`}
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>

      <button className="btn btn-ghost" style={{ marginTop: 12 }} onClick={() => addRow()}>
        + Add a card
      </button>
    </div>
  );
}

function toRow(card: Card): Row {
  return {
    id: card.id,
    front: card.front,
    back: card.back,
    // A card a note declares is rewritten from that note on every save.
    derived: Boolean(card.from_note),
    dirty: false,
  };
}
