import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxy = the console's trust boundary (nginx does the same in Docker).
// The browser never holds the admin token. The proxy adds it to exactly one call,
// POST /mandates/{id}/revoke, and refuses every other non-GET request. Revoking can
// only remove authority, so this is the one human action safe to expose on localhost.
// The optional /api: bypass() sees the browser's path, proxyReq sees it after rewrite().
const REVOKE = /^(\/api)?\/mandates\/mnd_[0-9A-HJKMNP-TV-Z]{26}\/revoke$/;

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, "..", ""), ...process.env };
  const target = env.MANDATE_URL || "http://localhost:8000";
  const admin = env.MANDATE_ADMIN_TOKEN || "";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target,
          rewrite: (p) => p.replace(/^\/api/, ""),
          bypass(req, res) {
            const ok = req.method === "GET" || (req.method === "POST" && REVOKE.test(req.url ?? ""));
            if (!ok && res) {
              res.statusCode = 403;
              res.end("The console may only read, and revoke.");
              return false;
            }
          },
          configure(proxy) {
            proxy.on("proxyReq", (proxyReq, req) => {
              proxyReq.removeHeader("authorization");
              if (req.method === "POST" && REVOKE.test(req.url ?? "") && admin) {
                proxyReq.setHeader("authorization", `Bearer ${admin}`);
              }
            });
          },
        },
      },
    },
  };
});
