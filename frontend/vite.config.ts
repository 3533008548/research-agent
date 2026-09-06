import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  // The production bundle is served by FastAPI under /app/. Vite's local
  // development server stays at / so opening http://localhost:5173 is simple.
  base: command === "build" ? "/app/" : "/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:7860",
    },
  },
}));
