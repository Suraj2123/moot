import { useEffect, useRef, useState } from "react";
import { askStream, ApiError, type AnswerPayload, type Source } from "../api";
import { CohortScope, SourceDialog } from "../components/CohortControls";
import { Alert } from "../components/ui";
import { IconSend } from "../components/Icons";

interface Turn {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  answer?: AnswerPayload;
  streaming?: boolean;
}

const SUGGESTIONS = [
  "What did I write about gradient descent?",
  "Summarise my notes on the alliance system",
  "What does my note say about learning rates?",
];

/**
 * Renders [N12] citations as chips.
 *
 * Ids the model invented are styled differently rather than hidden. A citation
 * that looks real and is not is the failure mode this whole feature is built to
 * avoid, so it is shown as wrong rather than quietly dropped.
 */
function Prose({ text, answer, sources, onSource }: { text: string; answer?: AnswerPayload; sources?: Source[]; onSource: (source: Source) => void }) {
  const invented = new Set(answer?.invented_note_ids ?? []);
  const parts = text.split(/(\[N\d+\])/g);

  return (
    <div className="prose">
      {parts.map((part, i) => {
        const match = /^\[N(\d+)\]$/.exec(part);
        if (!match) return <span key={i}>{part}</span>;
        const id = Number(match[1]);
        const bad = invented.has(id);
        const source = sources?.find(s => s.note_id === id);
        return (
          <button
            type="button"
            disabled={bad || !source}
            onClick={() => source && onSource(source)}
            key={i}
            className={`citation${bad ? " invented" : ""}`}
            title={bad ? "This note was never supplied — the answer invented it" : `Note ${id}`}
          >
            N{id}{bad ? " ?" : ""}{source?.contributor_name ? ` · ${source.contributor_name}` : ""}
          </button>
        );
      })}
    </div>
  );
}

/**
 * `initialQuestion` is what somebody typed into Mooty on the hub. It is sent
 * once, on mount, rather than dropped into the composer for them to press
 * enter on again -- they already pressed enter.
 */
export function ChatPage({ initialQuestion = "", initialCohort = null }: { initialQuestion?: string; initialCohort?: number | null } = {}) {
  const [scope, setScope] = useState<number | null>(initialCohort);
  const [first, setFirst] = useState(initialQuestion);
  return <ChatConversation key={scope ?? "private"} initialQuestion={first} scope={scope} onScope={id => { setFirst(""); setScope(id); }} />;
}

function ChatConversation({ initialQuestion, scope, onScope }: { initialQuestion: string; scope: number | null; onScope: (id: number | null) => void }) {
  const [source, setSource] = useState<Source | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Deliberately keyed on nothing: a question handed over from the hub is sent
  // exactly once, when this page appears with one.
  useEffect(() => {
    if (initialQuestion.trim()) send(initialQuestion);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;

    setError("");
    setQuestion("");
    setBusy(true);

    // The history sent to the API is the conversation *before* this question.
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((prev) => [
      ...prev,
      { role: "user", content: trimmed },
      { role: "assistant", content: "", streaming: true },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const event of askStream(trimmed, history, controller.signal, scope)) {
        setTurns((prev) => {
          const next = [...prev];
          const last = { ...next[next.length - 1] };
          if (event.type === "sources") last.sources = event.sources;
          else if (event.type === "text") last.content += event.text;
          else if (event.type === "done") {
            last.answer = event.answer;
            last.sources = event.answer?.sources ?? last.sources;
            last.streaming = false;
            // The server is the authority on the final text; a dropped frame
            // would otherwise leave a subtly truncated answer on screen.
            if (event.answer?.text) last.content = event.answer.text;
          } else if (event.type === "error") {
            // The server sends the reason -- out of credit, key rejected,
            // rate limited -- and every one of those is something the reader
            // can act on. Replacing it with "something went wrong" throws
            // away the only useful part of the failure.
            last.content =
              last.content ||
              event.message ||
              "Something went wrong generating that answer.";
            last.streaming = false;
          }
          next[next.length - 1] = last;
          return next;
        });
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
      setTurns((prev) => prev.slice(0, -2));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="chat-wrap">
      <CohortScope value={scope} onChange={onScope} disabled={busy} />
      {source && <SourceDialog source={source} onClose={() => setSource(null)} />}
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-inner">
          {turns.length === 0 ? (
            <div style={{ paddingTop: 40 }}>
              <h1 style={{ fontFamily: "var(--font-prose)", fontSize: 30, marginBottom: 8 }}>
                Ask Mooty
              </h1>
              <p className="muted" style={{ marginTop: 0, maxWidth: 460 }}>
                Mooty answers from your notes{scope ? " and this cohort’s shared notes" : ""}, with a source for each claim. If your notes do not cover it, it says so rather than guessing.
              </p>
              <div className="stack" style={{ marginTop: 22, maxWidth: 460 }}>
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="note-item" onClick={() => send(s)} style={{ textAlign: "left" }}>
                    <span className="muted">{s}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {turns.map((turn, i) => (
            <div className="msg" key={i}>
              <div className={`msg-avatar ${turn.role === "user" ? "you" : "ai"}`}>
                {turn.role === "user" ? "You" : "S"}
              </div>
              <div className="msg-body">
                {turn.role === "user" ? (
                  <div style={{ paddingTop: 2 }}>{turn.content}</div>
                ) : (
                  <>
                    {turn.content ? (
                      <Prose text={turn.content} answer={turn.answer} sources={turn.sources} onSource={setSource} />
                    ) : (
                      <span className="dots" aria-label="Thinking">
                        <span /><span /><span />
                      </span>
                    )}

                    {turn.answer && !turn.answer.grounded ? (
                      <div style={{ marginTop: 10 }}>
                        <Alert kind="warn">
                          This answer cited {turn.answer.invented_note_ids.length} note
                          {turn.answer.invented_note_ids.length === 1 ? "" : "s"} that were
                          never supplied to it. Treat the marked citations as unverified.
                        </Alert>
                      </div>
                    ) : null}

                    {turn.sources?.length ? (
                      <div className="sources">
                        {turn.sources.map((s) => (
                          <button className="source-chip" key={s.note_id} title={`Read note ${s.note_id}`} onClick={() => setSource(s)}>
                            <span className="mono">N{s.note_id}</span> {s.title}{s.contributor_name ? ` · ${s.contributor_name}` : ""}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            </div>
          ))}

          {error ? <Alert>{error}</Alert> : null}
        </div>
      </div>

      <div className="chat-composer">
        <div className="composer-inner">
          <textarea
            className="textarea"
            placeholder="Ask Mooty about your notes…"
            value={question}
            rows={1}
            disabled={busy}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends, Shift+Enter makes a newline -- the convention every
              // chat app shares, so breaking it would be its own surprise.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(question);
              }
            }}
          />
          <button
            className="btn btn-primary composer-send"
            onClick={() => send(question)}
            disabled={busy || !question.trim()}
            aria-label="Send"
          >
            <IconSend />
          </button>
        </div>
        <p className="small faint" style={{ maxWidth: 760, margin: "8px auto 0" }}>
          Grounded in your notes — not a general assistant. Answers can still be wrong if
          your notes are.
        </p>
      </div>
    </div>
  );
}
