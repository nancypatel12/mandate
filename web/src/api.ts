// Types mirror the mandate service's JSON. The console reads, and revokes. Nothing else.

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

export async function revoke(id: string): Promise<void> {
  const r = await fetch(`/api/mandates/${id}/revoke`, { method: "POST" });
  if (!r.ok) throw new Error(`revoke: HTTP ${r.status} ${await r.text()}`);
}

export const usd = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
