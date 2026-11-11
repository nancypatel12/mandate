import type { IncomingMessage, ServerResponse } from "node:http";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxy = the console's trust boundary (nginx does the same in Docker).
// The console is the human's seat: it can read, sign a mandate, revoke one, and start
// the agent. The browser never holds the admin token. The proxy adds it to exactly two
// calls (sign, revoke) and refuses every other write to the mandate service.
// Writes must carry X-Mandate-Console: 1. A browser won't send a custom header across
// origins without a preflight, and nothing here answers one, so another site open in
// the same browser can't sign or revoke.
// The optional /api: bypass() sees the browser's path, proxyReq sees it after rewrite().
const REVOKE = /^(\/api)?\/mandates\/mnd_[0-9A-HJKMNP-TV-Z]{26}\/revoke$/;
const SIGN = /^(\/api)?\/mandates$/;
const START_RUN = /^(\/agent)?\/runs$/;

const fromConsole = (req: IncomingMessage) => req.headers["x-mandate-console"] === "1";
const humanWrite = (req: IncomingMessage) =>
  req.method === "POST" && (REVOKE.test(req.url ?? "") || SIGN.test(req.url ?? ""));

function refuse(res: ServerResponse | undefined, why: string) {
  if (res) {
    res.statusCode = 403;
    res.end(why);
  }
  return false as const;
}

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, "..", ""), ...process.env };
  const target = env.MANDATE_URL || "http://localhost:8000";
  const agent = env.AGENT_URL || "http://localhost:8001";
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
            const ok = req.method === "GET" || (humanWrite(req) && fromConsole(req));
            if (!ok) return refuse(res, "The console may only read, sign, and revoke.");
          },
          configure(proxy) {
            proxy.on("proxyReq", (proxyReq, req) => {
              proxyReq.removeHeader("authorization");
              if (humanWrite(req) && admin) proxyReq.setHeader("authorization", `Bearer ${admin}`);
            });
          },
        },
        // The agent runner. It gets no admin token: only what the browser sends.
        "/agent": {
          target: agent,
          rewrite: (p) => p.replace(/^\/agent/, ""),
          bypass(req, res) {
            const start = req.method === "POST" && START_RUN.test(req.url ?? "") && fromConsole(req);
            if (req.method !== "GET" && !start) return refuse(res, "The console may only start and watch runs.");
          },
          configure(proxy) {
            proxy.on("proxyReq", (proxyReq) => proxyReq.removeHeader("authorization"));
          },
        },
      },
    },
  };
});
