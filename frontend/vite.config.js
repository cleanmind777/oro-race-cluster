import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev only: 127.0.0.1:5173 proxies /api to oro-main on 8080.
// Production serves the build from oro-main itself, so nothing listens on 5173.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.MAIN_URL || "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
