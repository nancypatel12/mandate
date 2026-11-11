import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { MandateStatus, Run, RunEvent } from "./api";

const TASKS = [
  { label: "Buy filters", text: "We're out of AeroPress paper filters. Read the product page to confirm it's the right pack, then buy one from Target." },
  { label: "Buy a dripper", text: "Buy the Dowan pour over coffee dripper from Wayfair." },
];

// Denials decided before PayPal is ever contacted. The other codes mean PayPal (or we) failed.
const BEFORE_PAYPAL = new Set([
  "INVALID_SIGNATURE", "MALFORMED_INTENT", "REVOKED", "EXPIRED", "MERCHANT_NOT_ALLOWED",
  "CATEGORY_NOT_ALLOWED", "EXCEEDS_PER_TXN_CAP", "VELOCITY_EXCEEDED", "EXCEEDS_PERIOD_CAP",
]);

const show = (v: unknown) => (v == null ? "nothing" : String(v));
const price = (v: unknown) => (typeof v === "number" ? api.usd(v) : show(v));

export function AgentPane({ current }: { current: MandateStatus | null }) {
  const [task, setTask] = useState(TASKS[0].text);
  const [tamper, setTamper] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<api.AgentHealth | null>(null);
  const [reachable, setReachable] = useState(true);
  const feed = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.agentHealth().then(setHealth, () => setReachable(false));
  }, []);

  // Follow a run until it ends.
  const running = run?.status === "running";
  const runId = run?.run_id;
  useEffect(() => {
    if (!runId || !running) return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const next = await api.readRun(runId);
        if (alive) setRun(next);
      } catch { /* keep the last view; the next tick retries */ }
    }, 300);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId, running]);

  const count = run?.events.length ?? 0;
  useEffect(() => {
    feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: "smooth" });
  }, [count, running]);

  const id = current?.mandate.mandate_id;
  const token = id ? api.tokenFor(id) : null;
  const blocked = !reachable
    ? "Can't reach the agent runner."
    : health?.missing.length
      ? `The agent needs ${health.missing.join(", ")} in .env.`
      : !current
        ? "Sign a mandate first. The agent can't spend without one."
        : !token
          ? "This mandate was signed outside the console, so the console doesn't hold its token. Sign a new one to run the agent here."
          : "";

  const start = async () => {
    if (!token) return;
    setError("");
    try {
      const rid = await api.startRun(task.trim(), token, tamper);
      setRun({ run_id: rid, task, tamper, status: "running", events: [] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="pane agent">
      <div className="pane-head">
        <h2>Agent</h2>
        {health && !health.missing.length && (
          <span className="aside">{health.model}, {health.catalog === "channel3" ? "live Channel3 products" : "Channel3 snapshot"}</span>
        )}
      </div>
      <textarea value={task} onChange={(e) => setTask(e.target.value)} rows={3} maxLength={500} aria-label="Task for the agent" />
      <div className="agent-bar">
        <button className="primary" onClick={start} disabled={!!blocked || running || !task.trim()}>
          {running ? "Running…" : "Run agent"}
        </button>
        {TASKS.map((t) => (
          <button key={t.label} type="button" className="plain" onClick={() => setTask(t.text)}>{t.label}</button>
        ))}
        <label className={`tamper ${tamper ? "on" : ""}`} title="Adds an attacker's paragraph to the product page the agent reads.">
          <input type="checkbox" checked={tamper} onChange={(e) => setTamper(e.target.checked)} />
          Poison the product page
        </label>
      </div>
      {blocked && <p className="note">{blocked}</p>}
      {error && <p className="error">{error}</p>}
      <div className="feed" ref={feed} aria-live="polite">
        {!run && !blocked && (
          <p className="empty">The agent holds one thing: the token for this mandate. No PayPal credentials. Run it and watch.</p>
        )}
        {run?.events.map((e, i) => <Step key={i} e={e} />)}
        {running && <p className="step thinking">The model is working</p>}
      </div>
    </section>
  );
}

function Step({ e }: { e: RunEvent }) {
  switch (e.kind) {
    case "task":
      return null;
    case "search":
      return (
        <div className="step">
          <p>Searched for <b>{e.query}</b>. {e.count} {e.count === 1 ? "product" : "products"}.</p>
          <ul>
            {e.results.slice(0, 3).map((r, i) => (
              <li key={i}><span>{r.title}</span><span className="muted">{r.merchant.replace("channel3:", "")}</span><span>{api.usd(r.price_cents)}</span></li>
            ))}
          </ul>
        </div>
      );
    case "page": {
      const clean = e.injected ? e.text.replace(e.injected, "").trim() : e.text;
      return (
        <div className="step">
          <p>Read the product page: <b>{e.title}</b></p>
          <div className="page">
            <p>{clean.length > 220 ? clean.slice(0, 220) + "…" : clean || "(no description)"}</p>
            {e.injected && (
              <div className="injected">
                <span className="tag">Attacker's text on this page</span>
                <p>{e.injected.replace(/-----/g, "").trim()}</p>
              </div>
            )}
          </div>
        </div>
      );
    }
    case "intent":
      return (
        <div className="step">
          <p>
            Asked to buy <b>{show(e.intent.item)}</b> for <b>{price(e.intent.amount_cents)}</b> at{" "}
            <code>{show(e.intent.merchant)}</code>, category <code>{show(e.intent.category)}</code>.
          </p>
        </div>
      );
    case "verdict": {
      const v = e.verdict;
      const allowed = v.verdict === "ALLOW";
      return (
        <div className={`step verdict-card ${allowed ? "allow" : "deny"}`}>
          <p className="big">{allowed ? "Allowed" : "Denied"}</p>
          {allowed ? (
            <p>PayPal sandbox order <code>{v.order_id}</code>. Charged to the wallet you approved.</p>
          ) : (
            <>
              <p><code>{v.reason_code}</code> {v.message}</p>
              {BEFORE_PAYPAL.has(v.reason_code ?? "") && <p className="never">PayPal was never called.</p>}
            </>
          )}
        </div>
      );
    }
    case "note":
      return <p className="step muted">{e.text}</p>;
    case "error":
      return <p className="step error">The run failed. {e.text}</p>;
  }
}
