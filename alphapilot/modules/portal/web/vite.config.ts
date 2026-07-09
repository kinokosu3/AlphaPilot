import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalized = id.replace(/\\/g, "/");
          if (
            normalized.includes("/node_modules/@plotly/") ||
            normalized.includes("/node_modules/plotly.js") ||
            normalized.includes("/node_modules/plotly.js-dist-min") ||
            normalized.includes("/node_modules/react-plotly.js")
          ) {
            return "charts";
          }
        }
      }
    }
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:19901",
      "/branding": "http://127.0.0.1:19901",
    }
  }
});
