// Types mirror the mandate service's and the agent runner's JSON.
// The console reads, signs, revokes, and starts the agent. The proxy enforces that.

export type LedgerRow = {
  seq: number;
  ledger_id: string;
  ts: string;
  mandate_id: string | null;
  intent_id: string | null;
  merchant: string | null;
  category: string | null;
  amount_cents: number | null;
  item: string | null;
  verdict: "ALLOW" | "DENY";
  reason_code: string | null;
  message: string;
  order_id: string | null;
};

export type MandateStatus = {
  mandate: {
    mandate_id: string;
    subject: string;
    issuer: string;
    iat: number;
    exp: number;
    merchant_allowlist: string[];
    category_allowlist: string[];
    per_txn_cap_cents: number;
    period: "day" | "week" | "month";
    period_cap_cents: number;
    max_txn_per_period: number;
  };
  issued_at: string;
  revoked_at: string | null;
  period_start: string;
  spend: { spent_cents: number; txn_count: number };
};

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

export const ledger = () => get<LedgerRow[]>("/ledger?limit=200");
export const mandates = () => get<MandateStatus[]>("/mandates?limit=20");

// Writes carry this header. The proxy refuses writes without it (see vite.config.ts).
const WRITE = { "X-Mandate-Console": "1" };

export async function revoke(id: string): Promise<void> {
  const r = await fetch(`/api/mandates/${id}/revoke`, { method: "POST", headers: WRITE });
  if (!r.ok) throw new Error(`revoke: HTTP ${r.status} ${await r.text()}`);
}

export type NewMandate = {
  agent: string;
  user: string;
  merchants: string[];
  categories: string[];
  per_txn_cap_cents: number;
  period: "day" | "week" | "month";
  period_cap_cents: number;
  max_txn_per_period: number;
  ttl_seconds: number;
};

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { ...WRITE, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = await r.text();
    try {
      const d = JSON.parse(detail).detail;
      detail = typeof d === "string" ? d : "Check the fields and try again.";
    } catch { /* plain text */ }
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// Signing returns the mandate token once. The console keeps it for this tab only,
// so it can hand it to the agent: the token is all the agent ever gets.
export async function sign(m: NewMandate): Promise<string> {
  const out = await post<{ token: string; mandate: { mandate_id: string } }>("/api/mandates", m);
  try {
    sessionStorage.setItem(`mandate-token:${out.mandate.mandate_id}`, out.token);
  } catch { /* private window: the agent pane will ask for a new mandate */ }
  return out.mandate.mandate_id;
}

export function tokenFor(id: string): string | null {
  try {
    return sessionStorage.getItem(`mandate-token:${id}`);
  } catch {
    return null;
  }
}

// ── the agent runner ─────────────────────────────────────────────────

export type Verdict = {
  verdict: "ALLOW" | "DENY";
  reason_code: string | null;
  message: string;
  order_id: string | null;
};

export type RunEvent =
  | { kind: "task"; text: string }
  | { kind: "search"; query: string; count: number; results: { title: string; merchant: string; price_cents: number }[] }
  | { kind: "page"; title: string; merchant: string; price_cents: number; text: string; injected: string | null }
  | { kind: "intent"; intent: { merchant: unknown; category: unknown; amount_cents: unknown; item: unknown } }
  | { kind: "verdict"; verdict: Verdict }
  | { kind: "note"; text: string }
  | { kind: "error"; text: string };

export type Run = { run_id: string; task: string; tamper: boolean; status: "running" | "done" | "error"; events: RunEvent[] };
export type AgentHealth = { ok: boolean; missing: string[]; model: string; catalog: "channel3" | "snapshot" };

export const agentHealth = async (): Promise<AgentHealth> => {
  const r = await fetch("/agent/healthz");
  if (!r.ok) throw new Error(`agent: HTTP ${r.status}`);
  return r.json();
};

export const startRun = (task: string, token: string, tamper: boolean) =>
  post<{ run_id: string }>("/agent/runs", { task, token, tamper }).then((r) => r.run_id);

export async function readRun(id: string): Promise<Run> {
  const r = await fetch(`/agent/runs/${id}`);
  if (!r.ok) throw new Error(`run: HTTP ${r.status}`);
  return r.json();
}

export const usd = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
