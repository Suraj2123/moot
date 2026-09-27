import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets land in ../studylink/static, which FastAPI serves. One process
// in production, so there is no CORS to configure and no second thing to deploy.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "../studylink/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // In development the API runs separately on 8000, so proxy rather than
    // enabling CORS -- the browser sees one origin and production behaves the
    // same way as dev.
    proxy: Object.fromEntries(
      [
        "/auth", "/notes", "/courses", "/assignments", "/search", "/ask",
        "/canvas", "/jobs", "/sync", "/reindex", "/usage", "/health",
        "/evaluation", "/work-session",
        // Added later than the list above, and each one was invisible in dev
        // until it was: an unproxied path is served by Vite, which answers
        // with index.html and a JSON parse error rather than a 404.
        "/decks", "/cards", "/outline", "/progress", "/discover", "/d",
        "/model-key", "/pricing", "/cohorts", "/cohort-invites",
      ].map((path) => [path, { target: "http://127.0.0.1:8000", changeOrigin: true }])
    ),
  },
});
