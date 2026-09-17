import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "/app/" -- app.py serves this build's index.html at both /login and
// every /app/* path (SPA fallback), but assets must always resolve the same
// way regardless of which of those the browser is currently on.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "dist",
  },
  server: {
    // `npm run dev` proxies API calls to the real backend so the app can be
    // developed without rebuilding on every change; app.py itself is what
    // serves the production build (see README in this directory).
    proxy: {
      "/api": "http://127.0.0.1:8766",
      "/stream": "http://127.0.0.1:8766",
    },
  },
});
