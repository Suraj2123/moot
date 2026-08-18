# StudyLink web

React + TypeScript, built with Vite. No component framework and no state
library: the app has four screens and one piece of shared state (who is signed
in), so a router and a store would both be more machinery than the problem.

## Running it

```bash
# once
npm install

# development -- Vite on 5173, proxying the API on 8000
npm run dev

# production -- builds into ../studylink/static, which FastAPI serves
npm run build
```

In production the API serves the built app from the same origin, so there is no
CORS to configure. The proxy in `vite.config.ts` exists so development behaves
the same way rather than needing CORS turned on for a second origin.

## Layout

```
src/api.ts             every call to the API, and the session token
src/App.tsx            shell, navigation, theme, sign-in gate
src/pages/Auth.tsx     sign in and create account
src/pages/Chat.tsx     ask your notes, streamed
src/pages/Notes.tsx    write notes, see what each is relevant to
src/pages/Assignments  assignments and matching notes, with the evidence
src/pages/Settings.tsx Canvas, background jobs, AI spend, theme
src/components/        shared pieces
src/styles.css         the design system, as CSS variables
```

## Two decisions worth knowing

**The session token is in localStorage.** Any injected script can read it, which
is why `docs/AUTH.md` lists XSS as undefended. The alternative -- an httpOnly
cookie -- needs CSRF protection the API does not have yet, and picking the
weaker option knowingly beats bolting on half of the stronger one.

**Invented citations are shown, not hidden.** When the chat cites a note it was
never given, the citation renders in red and a warning appears above the
sources. Quietly dropping it would make an ungrounded answer look clean, which
is exactly the failure the grounding work exists to surface.
