import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy API + SSE to the backend during development so the frontend stays a
// pure SPA that only talks to the generated OpenAPI surface.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
      "/share": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
