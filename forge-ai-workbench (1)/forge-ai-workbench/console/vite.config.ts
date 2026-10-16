import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console talks only to the AI Workbench API (/v1). Locally the dev server proxies /v1 to the API
// (AIWB_API_URL, default http://127.0.0.1:8000); in Azure the console container's nginx does the same.
const api = process.env.AIWB_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/v1": { target: api, changeOrigin: true } },
  },
  preview: {
    port: 4173,
    proxy: { "/v1": { target: api, changeOrigin: true } },
  },
});
