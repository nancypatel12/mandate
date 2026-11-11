import { useState, type FormEvent } from "react";
import * as api from "./api";

// "target.com" → "channel3:target-com": the id Channel3 results carry and a mandate allowlists.
const merchantId = (raw: string) => {
  const s = raw.trim().toLowerCase();
  if (s.startsWith("channel3:")) return s;
  return "channel3:" + s.replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
};
const slug = (raw: string) => raw.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const list = (raw: string) => raw.split(",").map((x) => x.trim()).filter(Boolean);
const cents = (raw: string) => Math.round(Number(raw) * 100);

const LASTS = [
  { label: "1 hour", seconds: 3600 },
  { label: "2 hours", seconds: 7200 },
  { label: "1 day", seconds: 86_400 },
  { label: "7 days", seconds: 7 * 86_400 },
  { label: "30 days", seconds: 30 * 86_400 },
];

export function SignForm(props: { onSigned: (id: string) => void; onCancel?: () => void }) {
  const [agent, setAgent] = useState("shopper");
  const [user, setUser] = useState("nancy");
  const [stores, setStores] = useState("target.com, wayfair.com");
  const [categories, setCategories] = useState("coffee-filters, drippers");
  const [perTxn, setPerTxn] = useState("15");
  const [perPeriod, setPerPeriod] = useState("40");
  const [period, setPeriod] = useState<"day" | "week" | "month">("day");
  const [maxTxn, setMaxTxn] = useState("3");
  const [ttl, setTtl] = useState(7200);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const lasts = LASTS.find((l) => l.seconds === ttl)!.label;
  const money = (raw: string) => (Number(raw) > 0 ? api.usd(cents(raw)) : "$…");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const id = await api.sign({
        agent: slug(agent),
        user: slug(user),
        merchants: list(stores).map(merchantId),
        categories: list(categories).map(slug),
        per_txn_cap_cents: cents(perTxn),
        period,
        period_cap_cents: cents(perPeriod),
        max_txn_per_period: Math.round(Number(maxTxn)),
        ttl_seconds: ttl,
      });
      props.onSigned(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setBusy(false);
  };

  return (
    <form className="sign" onSubmit={submit}>
      <label>Agent<input value={agent} onChange={(e) => setAgent(e.target.value)} required /></label>
      <label>Wallet owner<input value={user} onChange={(e) => setUser(e.target.value)} required /></label>
      <label className="wide">Stores<input value={stores} onChange={(e) => setStores(e.target.value)} required /></label>
      <label className="wide">Categories<input value={categories} onChange={(e) => setCategories(e.target.value)} required /></label>
      <label>Per purchase, $<input inputMode="decimal" value={perTxn} onChange={(e) => setPerTxn(e.target.value)} required /></label>
      <label>Purchases per {period}<input inputMode="numeric" value={maxTxn} onChange={(e) => setMaxTxn(e.target.value)} required /></label>
      <label>Total per {period}, $<input inputMode="decimal" value={perPeriod} onChange={(e) => setPerPeriod(e.target.value)} required /></label>
      <label>Period
        <select value={period} onChange={(e) => setPeriod(e.target.value as typeof period)}>
          <option value="day">Day</option><option value="week">Week</option><option value="month">Month</option>
        </select>
      </label>
      <label className="wide">Lasts
        <select value={ttl} onChange={(e) => setTtl(Number(e.target.value))}>
          {LASTS.map((l) => <option key={l.seconds} value={l.seconds}>{l.label}</option>)}
        </select>
      </label>
      <p className="wide reads">
        <b>{agent || "…"}</b> may spend up to <b>{money(perTxn)}</b> a purchase, <b>{money(perPeriod)}</b> a {period},{" "}
        <b>{maxTxn || "…"}</b> purchases a {period}, only at <b>{list(stores).join(", ") || "…"}</b>, only on{" "}
        <b>{list(categories).join(", ") || "…"}</b>, for <b>{lasts}</b>. Paid from {user || "…"}'s PayPal sandbox wallet.
      </p>
      <div className="wide actions">
        <button className="primary" disabled={busy}>{busy ? "Signing…" : "Sign mandate"}</button>
        {props.onCancel && <button type="button" className="plain" onClick={props.onCancel}>Cancel</button>}
      </div>
      {error && <p className="wide error">{error}</p>}
    </form>
  );
}
