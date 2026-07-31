import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-only proxy so `npm run dev` on :5173 can talk to the Python backend
// (default :8000, per config.py's mcp_port) without CORS.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/healthz": { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
  },
});
