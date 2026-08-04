import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const apiTarget = environment.VITE_API_TARGET ?? "http://127.0.0.1:8001";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/v1": apiTarget,
        "/health": apiTarget
      }
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            const contains = (value: string) => id.indexOf(value) !== -1;
            if (!contains("node_modules")) return undefined;
            if (contains("/react-dom/") || contains("/react/") || contains("/scheduler/")) {
              return "react-runtime";
            }
            if (
              contains("/react-markdown/") ||
              contains("/remark-gfm/") ||
              contains("/remark-parse/") ||
              contains("/unified/") ||
              contains("/micromark") ||
              contains("/mdast-") ||
              contains("/hast-")
            ) {
              return "markdown";
            }
            if (contains("/lucide-react/")) return "icons";
            return undefined;
          }
        }
      }
    }
  };
});
