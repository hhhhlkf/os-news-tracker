import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    watch: {
      usePolling: true,
    },
    proxy: {
      "/items": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/facets": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/run-logs": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/sources": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/sources/agent": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/crawl-sources": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/discovery": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/mail": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/templates": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/settings": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/embedding": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/cards": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/vectors": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/clusters": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/storylines": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/runs": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/results": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/trends/schedule": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/system-morning-crawl": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/auth": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/wechat-auth/profile": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/wechat-auth/qr-sessions": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/statistics/token-usage": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/statistics/item-volume": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
    },
  },
});
