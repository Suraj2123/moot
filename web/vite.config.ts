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
      ].map((path) => [path, { target: "http://127.0.0.1:8000", changeOrigin: true }])
    ),
  },
});
