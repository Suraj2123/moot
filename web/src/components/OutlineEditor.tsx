import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { study, type OutlineCard } from "../api";

/**
 * A textarea that behaves like an outliner and shows what cards the text
 * declares.
 *
 * Deliberately a textarea rather than a rich editor. A contenteditable outliner
 * is a large amount of machinery -- selection handling, undo, paste
 * normalisation, IME support -- and every bit of it is a way to lose someone's
 * notes. Plain text with Tab handling gets the part that matters, and what is
 * stored stays something a student could open in any other program.
 */
export function OutlineEditor({
  id, value, onChange, disabled, placeholder, minHeight = 220,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  placeholder?: string;
  minHeight?: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [cards, setCards] = useState<OutlineCard[] | null>(null);
  const [showCards, setShowCards] = useState(false);

  // Where the caret should end up after the next render, when this component
  // rewrote the text itself.
  //
  // It has to be applied in a layout effect rather than a requestAnimationFrame
  // callback. rAF runs after paint, which leaves a window where the value has
  // updated but the caret has not moved -- and a keystroke arriving in that
  // window is inserted at the old position. Typing at speed after a Tab or a
  // newline silently loses characters, which is about the worst failure an
  // editor can have.
  const pendingSelection = useRef<[number, number] | null>(null);

  useLayoutEffect(() => {
    const area = ref.current;
    const pending = pendingSelection.current;
    if (!area || !pending) return;
    pendingSelection.current = null;
    area.selectionStart = pending[0];
    area.selectionEnd = pending[1];
  }, [value]);

  // Parsed server-side so the editor and the save agree by construction --
  // a second implementation in TypeScript would drift, and the count would
  // start lying about what is actually stored.
  useEffect(() => {
    if (!value.trim()) { setCards([]); return; }
    let alive = true;
    const timer = setTimeout(() => {
      study.preview(value)
        .then((body) => alive && setCards(body.cards))
        .catch(() => alive && setCards(null));
    }, 350);
    return () => { alive = false; clearTimeout(timer); };
  }, [value]);

  function indent(outdent: boolean) {
    const area = ref.current;
    if (!area) return;
    const { selectionStart, selectionEnd } = area;
    const before = value.slice(0, selectionStart);
    const lineStart = before.lastIndexOf("\n") + 1;
    const selected = value.slice(lineStart, selectionEnd);

    const lines = selected.split("\n");
    const changed = lines.map((line) =>
      outdent ? line.replace(/^(\t| {1,2})/, "") : "  " + line,
    );
    const delta = changed.join("\n").length - selected.length;

    // Restore the selection, or the caret jumps to the end on every Tab and
    // the editor becomes unusable for exactly the people using Tab.
    pendingSelection.current = [
      Math.max(lineStart, selectionStart + (outdent ? -2 : 2)),
      selectionEnd + delta,
    ];
    onChange(value.slice(0, lineStart) + changed.join("\n") + value.slice(selectionEnd));
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Tab") {
      event.preventDefault();
      indent(event.shiftKey);
      return;
    }
    if (event.key === "Enter") {
      // Carry the current indentation onto the new line. Without it every
      // nested list flattens itself the moment you press return.
      const area = ref.current;
      if (!area) return;
      const before = value.slice(0, area.selectionStart);
      const line = before.slice(before.lastIndexOf("\n") + 1);
      const lead = line.match(/^[\t ]*/)?.[0] ?? "";
      if (!lead) return;
      event.preventDefault();
      const at = area.selectionStart;
      const caret = at + 1 + lead.length;
      pendingSelection.current = [caret, caret];
      onChange(value.slice(0, at) + "\n" + lead + value.slice(area.selectionEnd));
    }
  }

  const count = cards?.length ?? 0;

  return (
    <div>
      <textarea
        ref={ref}
        id={id}
        className="textarea outline-editor"
        style={{ minHeight }}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        spellCheck
      />

      <div className="between" style={{ marginTop: 8 }}>
        <span className="small faint">
          <code>term :: definition</code> makes a card ·{" "}
          <code>:::</code> both ways · <code>{"{{cloze}}"}</code> hides a word ·
          Tab indents
        </span>
        {count > 0 ? (
          <button className="btn btn-ghost btn-sm" onClick={() => setShowCards((v) => !v)}>
            {count} card{count === 1 ? "" : "s"} {showCards ? "▴" : "▾"}
          </button>
        ) : null}
      </div>

      {showCards && cards ? (
        <div className="stack" style={{ marginTop: 10 }}>
          {cards.map((card, i) => (
            <div className="upload-row" key={`${card.source_key}-${i}`}>
              <div style={{ minWidth: 0 }}>
                <div className="upload-name">{card.front}</div>
                <div className="small faint">{card.back}</div>
              </div>
              <span className="badge">{card.kind.replace("-", " ")}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
