import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy keeps everything same-origin (CSP-friendly, §114):
// /api -> REST backend, /ws -> WebSocket backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: { outDir: "dist" },
});
