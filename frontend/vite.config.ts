import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**"],
  },
  build: {
    // The service serves this from /app/frontend/dist (see Dockerfile).
    // No manualChunks: react-leaflet pulls react in with it, so hand-splitting
    // "vendor-react" just produced an empty 38-byte stub while react shipped
    // inside the leaflet chunk anyway. Rollup's default split is honest here.
    outDir: "dist",
  },
  server: {
    // `npm run dev` talks to a locally-running `uvicorn app:app`.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
