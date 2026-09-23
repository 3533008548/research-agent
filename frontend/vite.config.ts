import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  // FastAPI serves the production bundle from the root path. Vite's local
  // development server uses the same URLs, so links and assets stay portable.
  base: "/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:7860",
    },
  },
}));
