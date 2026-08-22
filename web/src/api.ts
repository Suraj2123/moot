/**
 * The one place the frontend talks to the API.
 *
 * Two decisions worth naming.
 *
 * The token lives in memory with a copy in localStorage. localStorage is
 * readable by any script that gets injected, which is a real risk and the
 * reason docs/AUTH.md lists XSS as undefended -- the alternative, an
 * httpOnly cookie, needs CSRF protection the API does not currently have, and
 * choosing the weaker option knowingly beats bolting on half of the stronger
 * one. It is written down rather than hidden.
 *
 * Every response goes through `handle`, so a 401 clears the session exactly
 * once, in one place. Scattering that across call sites is how an app ends up
 * with a page that renders forever against a token the server stopped
 * accepting.
 */

const TOKEN_KEY = "studylink.token";

let token: string | null = localStorage.getItem(TOKEN_KEY);
let onUnauthorized: (() => void) | null = null;

export function setToken(next: string | null) {
  token = next;
  if (next) localStorage.setItem(TOKEN_KEY, next);
  else localStorage.removeItem(TOKEN_KEY);
}

export function getToken() {
  return token;
}

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function headers(): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json" };
  if (token) base.Authorization = `Bearer ${token}`;
  return base;
}

async function handle<T>(response: Response): Promise<T> {
  if (response.status === 401) {
    setToken(null);
    onUnauthorized?.();
    throw new ApiError(401, "Your session has expired. Please sign in again.");
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
      // FastAPI validation errors arrive as a list of objects, which render as
      // "[object Object]" if passed straight to a toast.
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: any) => d.msg).join(", ");
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const api = {
  get: <T>(path: string) => fetch(path, { headers: headers() }).then(handle<T>),
  post: <T>(path: string, body?: unknown) =>
    fetch(path, { method: "POST", headers: headers(), body: JSON.stringify(body ?? {}) }).then(handle<T>),
  patch: <T>(path: string, body?: unknown) =>
    fetch(path, { method: "PATCH", headers: headers(), body: JSON.stringify(body ?? {}) }).then(handle<T>),
  del: <T>(path: string) => fetch(path, { method: "DELETE", headers: headers() }).then(handle<T>),

  /**
   * Multipart, so no Content-Type header of our own: the browser has to set
   * it in order to append the multipart boundary. Setting it by hand here is
   * the classic way to make every upload fail to parse server-side.
   */
  upload: <T>(path: string, form: FormData) => {
    const auth: Record<string, string> = {};
    if (token) auth.Authorization = `Bearer ${token}`;
    return fetch(path, { method: "POST", headers: auth, body: form }).then(handle<T>);
  },
};

/** What the server will accept, so the picker and the drop zone agree with it. */
export const UPLOAD_ACCEPT = ".md,.markdown,.txt,.text,.pdf,.docx";
export const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

export interface UploadResult {
  id: number;
  job: Job;
  kind: string;
  chars: number;
  /** Set when the file parsed but something is worth saying -- e.g. truncation. */
  notice: string | null;
}

export function uploadNote(
  file: File,
  opts: { title?: string; courseId?: number | null; sourceType?: string } = {},
) {
  const form = new FormData();
  form.append("file", file);
  if (opts.title?.trim()) form.append("title", opts.title.trim());
  if (opts.courseId != null) form.append("course_id", String(opts.courseId));
  if (opts.sourceType) form.append("source_type", opts.sourceType);
  return api.upload<UploadResult>("/notes/upload", form);
}

/* --------------------------------------------------------------- types */

export interface User { id: number; email: string | null; auth_source?: string }
export interface Course { id: number; name: string; course_code: string | null }
export interface Assignment {
  id: number; name: string; course: string | null;
  due_at: string | null; points_possible: number | null;
}
export interface Note {
  id: number; title: string; course: string | null;
  source_type: string; chars: number;
  /** Whether retrieval can see this note yet. */
  index_status?: "indexed" | "queued" | "stale";
}
export interface Evidence {
  /** Terms the note and the assignment actually share -- the reason for the match. */
  overlapping_terms?: string[];
  snippet?: string;
  chunk_ordinal?: number;
}
export interface Match {
  note_id?: number;
  assignment_id?: number;
  title?: string;
  name?: string;
  course?: string | null;
  score: number;
  /** Calibrated 0-1, not a label. See ConfidenceBadge. */
  confidence: number;
  source_type?: string;
  evidence?: Evidence;
}
export interface Job {
  id: number; kind: string; status: string;
  created_at: string; finished_at: string | null; detail: string;
}
export interface CanvasStatus {
  connected: boolean; api_url?: string; verified_at?: string | null;
}
export interface Usage {
  calls: number; input_tokens: number; output_tokens: number;
  cost_usd: number; budget_usd: number | null; remaining_usd: number | null;
}
export interface Source { note_id: number; title: string; score?: number }
export interface AnswerPayload {
  text: string; refused: boolean; grounded: boolean;
  cited_note_ids: number[]; invented_note_ids: number[];
  sources: Source[];
}

/* ---------------------------------------------------------------- calls */

export const auth = {
  signup: (email: string, password: string) =>
    api.post<{ token: string; user: User }>("/auth/signup", { email, password }),
  login: (email: string, password: string) =>
    api.post<{ token: string; user: User }>("/auth/login", { email, password }),
  me: () => api.get<User>("/auth/me"),
  logout: () => api.post<{ ok: boolean }>("/auth/logout"),
};

export const notes = {
  list: (search = "") =>
    api.get<Note[]>(`/notes${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  create: (title: string, body: string, course_id?: number | null) =>
    api.post<{ id: number; job: Job }>("/notes", { title, body, course_id: course_id ?? null }),
  assignmentsFor: (id: number) => api.get<Match[]>(`/notes/${id}/assignments`),
  /** Only the fields present are changed; `reindexed` says whether the text moved. */
  update: (
    id: number,
    patch: { title?: string; body?: string; course_id?: number | null; clear_course?: boolean },
  ) => api.patch<{ id: number; reindexed: boolean; job: Job | null }>(`/notes/${id}`, patch),
  remove: (id: number) => api.del<void>(`/notes/${id}`),
  get: (id: number) => api.get<Note & { body: string }>(`/notes/${id}`),
};

export const courses = { list: () => api.get<Course[]>("/courses") };

export const assignments = {
  list: () => api.get<Assignment[]>("/assignments"),
  matches: (id: number) =>
    api.get<{ assignment: { id: number; name: string }; matches: Match[] }>(
      `/assignments/${id}/matches`
    ),
};

export const jobs = {
  list: () => api.get<Job[]>("/jobs"),
  get: (id: number) => api.get<Job>(`/jobs/${id}`),
};

export const canvas = {
  status: () => api.get<CanvasStatus>("/canvas"),
  connect: (api_url: string, api_token: string) =>
    api.post<CanvasStatus>("/canvas/connect", { api_url, api_token }),
  disconnect: () => api.del<{ connected: boolean }>("/canvas"),
  sync: () => api.post<Job>("/sync"),
};

export const usage = { summary: () => api.get<Usage>("/usage") };

export const reindex = () => api.post<Job>("/reindex");

/**
 * Stream an answer, yielding each server-sent event.
 *
 * fetch rather than EventSource: EventSource cannot send an Authorization
 * header or use POST, so it would mean putting the session token in a query
 * string, where it lands in access logs and browser history.
 */
export async function* askStream(
  question: string,
  history: { role: string; content: string }[],
  signal?: AbortSignal
): AsyncGenerator<any> {
  const response = await fetch("/ask/stream", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ question, history }),
    signal,
  });

  if (!response.ok || !response.body) {
    // Errors arrive before the stream opens, by design, so this is a normal
    // JSON error response and can be handled like any other.
    await handle(response);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. A partial frame stays in the
    // buffer until the rest of it arrives.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        yield JSON.parse(line.slice(6));
      } catch {
        /* a frame that is not JSON is not ours */
      }
    }
  }
}

/* ----------------------------------------------------------------- cards */

export interface Deck {
  id: number;
  title: string;
  course_id: number | null;
  created_at: string;
  cards: number;
  due: number;
}
export interface Card {
  id: number;
  deck_id: number;
  note_id: number | null;
  front: string;
  back: string;
  /** The sentence from the note that supports this card. */
  evidence: string;
  due_at: string | null;
  interval_days: number;
  ease: number;
  reviews: number;
  lapses: number;
}
export interface DeckDetail extends Deck {
  studied: number;
  items: Card[];
}
export interface TestQuestion {
  card_id: number;
  prompt: string;
  choices: string[];
  answer: string;
  kind: "multiple_choice" | "written";
}

/** 0 forgot, 1 hard, 2 good, 3 easy — matches studylink/cards.py. */
export const FORGOT = 0, HARD = 1, GOOD = 2, EASY = 3;

export const decks = {
  list: () => api.get<Deck[]>("/decks"),
  get: (id: number) => api.get<DeckDetail>(`/decks/${id}`),
  create: (note_id: number, count = 10, title?: string) =>
    api.post<{ deck_id: number; cards: number; rejected: number }>(
      "/decks", { note_id, count, title: title ?? null },
    ),
  remove: (id: number) => api.del<void>(`/decks/${id}`),
  study: (id: number) => api.get<Card[]>(`/decks/${id}/study`),
  review: (cardId: number, grade: number) =>
    api.post<{ card_id: number; interval_days: number; due_at: string }>(
      `/cards/${cardId}/review`, { grade },
    ),
  test: (id: number, kind: "multiple_choice" | "written" = "multiple_choice", length = 10) =>
    api.get<{ deck_id: number; questions: TestQuestion[] }>(
      `/decks/${id}/test?kind=${kind}&length=${length}`,
    ),
  check: (given: string, expected: string) =>
    api.post<{ verdict: "correct" | "close" | "wrong"; expected: string }>(
      "/cards/check", { given, expected },
    ),
};

/* --------------------------------------------------- outline & progress */

export interface OutlineCard {
  front: string;
  back: string;
  evidence: string;
  kind: string;
  source_key: string;
}
export interface CardPerformance {
  id: number;
  deck_id: number;
  front: string;
  back: string;
  attempts: number;
  correct: number;
  accuracy: number | null;
  lapses: number;
  interval_days: number;
  confidence?: number;
}
export interface Progress {
  cards: number;
  new: number;
  learning: number;
  mastered: number;
  attempts: number;
  correct: number;
  accuracy: number | null;
  streak_days: number;
  reviews_recent: number;
  activity: { day: string; reviews: number; correct: number }[];
}

export const study = {
  /** Parse without saving, so the editor can count cards as you type. */
  preview: (text: string) =>
    api.post<{ cards: OutlineCard[] }>("/outline/preview", { text }),
  progress: (deckId?: number) =>
    api.get<Progress>(`/progress${deckId ? `?deck_id=${deckId}` : ""}`),
  weak: (deckId?: number, limit = 10) =>
    api.get<CardPerformance[]>(
      `/progress/weak?limit=${limit}${deckId ? `&deck_id=${deckId}` : ""}`,
    ),
};
