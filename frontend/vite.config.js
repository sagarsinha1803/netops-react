import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev: vite on 5173, the FastAPI backend on 8000 -- proxy the socket + API.
// prod: `npm run build` -> dist/, served by FastAPI itself, no proxy involved.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/api": { target: "http://127.0.0.1:8000" },
    },
  },
});
