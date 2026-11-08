import { useCallback, useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import { colorSchemeLight, themeQuartz, type ColDef, type GetRowIdParams, type RowClassRules } from "ag-grid-community";
import * as api from "./api";
import type { LedgerRow, MandateStatus } from "./api";

const POLL_MS = 500; // revoke must show up in the grid within a second

// AG Grid in the portfolio look: Inter, black on white, hairline rules, accent only for focus.
const gridTheme = themeQuartz.withPart(colorSchemeLight).withParams({
  fontFamily: "Inter, -apple-system, sans-serif",
  fontSize: 14,
  foregroundColor: "#000",
  backgroundColor: "#fff",
  headerBackgroundColor: "#fff",
  headerTextColor: "#888",
  headerFontWeight: 400,
  borderColor: "#e6e6e6",
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowHoverColor: "#f4f4f6",
  oddRowBackgroundColor: "#fff",
  accentColor: "#3a4fbf",
  columnBorder: false,
  headerColumnBorder: false,
  headerColumnResizeHandleColor: "transparent",
  iconSize: 12,
  cellHorizontalPadding: 12,
});

export default function App() {
  const [rows, setRows] = useState<LedgerRow[]>([]);
  const [mandates, setMandates] = useState<MandateStatus[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [online, setOnline] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [l, m] = await Promise.all([api.ledger(), api.mandates()]);
        if (!alive) return;
        setRows(l);
        setMandates(m);
        setOnline(true);
      } catch {
        if (alive) setOnline(false);
      }
    };
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Follow the newest mandate unless the human picked one.
  const current = mandates.find((m) => m.mandate.mandate_id === picked) ?? mandates[0] ?? null;

  return (
    <div className="app">
      <header>
        <h1>MANDATE</h1>
        <p className="tagline">An AI agent can only spend what you signed for.</p>
        <p className={`status ${online ? "" : "off"}`}>
          {online ? "Live. Updates every half second." : "Can't reach the mandate service."}
        </p>
      </header>
      <div className="top">
        <MandatePane mandates={mandates} current={current} onPick={setPicked} />
        <SpendPane current={current} />
      </div>
      <LedgerPane rows={rows} />
    </div>
  );
}

// ── 1. the mandate you signed, and the kill switch ────────────────────

function stateOf(m: MandateStatus): "live" | "revoked" | "expired" {
  if (m.revoked_at) return "revoked";
  return Date.now() / 1000 >= m.mandate.exp ? "expired" : "live";
}

const STATE_LABEL = { live: "Live", revoked: "Revoked", expired: "Expired" };

function MandatePane(props: { mandates: MandateStatus[]; current: MandateStatus | null; onPick: (id: string) => void }) {
  const { mandates, current, onPick } = props;
  const [arming, setArming] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setArming(false);
    setError("");
  }, [current?.mandate.mandate_id]);

  if (!current) {
    return (
      <section className="pane">
        <h2>Mandate</h2>
        <p className="empty">No mandate signed yet. Run <code>./scripts/demo.sh</code> or <code>mandate issue</code>.</p>
      </section>
    );
  }
  const m = current.mandate;
  const state = stateOf(current);

  const onRevoke = async () => {
    // Two clicks: revocation can't be undone.
    if (!arming) return setArming(true);
    try {
      await api.revoke(m.mandate_id);
    } catch (e) {
      setError(String(e));
    }
    setArming(false);
  };

  return (
    <section className="pane">
      <div className="pane-head">
        <h2>Mandate</h2>
        <span className={`aside state ${state}`}>{STATE_LABEL[state]}</span>
      </div>
      {mandates.length > 1 && (
        <select value={m.mandate_id} onChange={(e) => onPick(e.target.value)} aria-label="Mandate">
          {mandates.map((x) => (
            <option key={x.mandate.mandate_id} value={x.mandate.mandate_id}>
              {x.mandate.mandate_id}, {STATE_LABEL[stateOf(x)].toLowerCase()}
            </option>
          ))}
        </select>
      )}
      <dl>
        <dt>Signed by</dt><dd>{m.issuer} for <b>{m.subject}</b></dd>
        <dt>Merchants</dt><dd className="chips">{m.merchant_allowlist.map((x) => <code key={x}>{x}</code>)}</dd>
        <dt>Categories</dt><dd className="chips">{m.category_allowlist.map((x) => <code key={x}>{x}</code>)}</dd>
        <dt>Limits</dt>
        <dd>
          {api.usd(m.per_txn_cap_cents)} per purchase, {api.usd(m.period_cap_cents)} per {m.period},{" "}
          {m.max_txn_per_period} purchases per {m.period}
        </dd>
        <dt>Expires</dt><dd>{new Date(m.exp * 1000).toLocaleString()}</dd>
        <dt>Id</dt><dd><code>{m.mandate_id}</code></dd>
      </dl>
      <button className={`revoke ${arming ? "armed" : ""}`} disabled={state === "revoked"} onClick={onRevoke}>
        {state === "revoked" ? "Revoked" : arming ? "Click again to revoke" : "Revoke mandate"}
      </button>
      {error && <p className="error">{error}</p>}
    </section>
  );
}

// ── 2. spend this period vs the caps ──────────────────────────────────

function Meter(props: { label: string; used: number; cap: number; fmt: (n: number) => string }) {
  const pct = Math.min(100, props.cap ? (props.used / props.cap) * 100 : 0);
  return (
    <div className="meter">
      <div className="meter-label">
        <span>{props.label}</span>
        <span><b>{props.fmt(props.used)}</b> of {props.fmt(props.cap)}</span>
      </div>
      <div className="bar" role="meter" aria-valuenow={props.used} aria-valuemin={0} aria-valuemax={props.cap}>
        <div className={pct >= 100 ? "full" : ""} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function SpendPane({ current }: { current: MandateStatus | null }) {
  if (!current) {
    return (
      <section className="pane">
        <h2>Spend</h2>
        <p className="empty">Nothing yet.</p>
      </section>
    );
  }
  const m = current.mandate;
  const reset = new Date(current.period_start);
  if (m.period === "day") reset.setUTCDate(reset.getUTCDate() + 1);
  else if (m.period === "week") reset.setUTCDate(reset.getUTCDate() + 7);
  else reset.setUTCMonth(reset.getUTCMonth() + 1);
  return (
    <section className="pane">
      <h2>Spend this {m.period}</h2>
      <Meter label="Money" used={current.spend.spent_cents} cap={m.period_cap_cents} fmt={api.usd} />
      <Meter label="Purchases" used={current.spend.txn_count} cap={m.max_txn_per_period} fmt={String} />
      <p className="note">Resets {reset.toLocaleString()}. Only allowed purchases count. Denials cost nothing.</p>
    </section>
  );
}

// ── 3. the ledger: every attempt, allowed and denied ──────────────────

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour12: false });
const blank = (v: unknown) => (v == null ? "" : String(v));

function LedgerPane({ rows }: { rows: LedgerRow[] }) {
  const columns = useMemo<ColDef<LedgerRow>[]>(
    () => [
      { field: "ts", headerName: "Time", width: 100, valueFormatter: (p) => time(p.value), sort: "desc" },
      { field: "merchant", headerName: "Merchant", flex: 1.2, minWidth: 190, valueFormatter: (p) => blank(p.value) },
      { field: "item", headerName: "Item", flex: 1.4, minWidth: 160, valueFormatter: (p) => blank(p.value) },
      {
        field: "amount_cents", headerName: "Amount", width: 110, type: "rightAligned",
        valueFormatter: (p) => (p.value == null ? "" : api.usd(p.value)),
      },
      {
        field: "verdict", headerName: "Verdict", width: 100,
        cellRenderer: (p: { value: string }) => <span className={`verdict ${p.value}`}>{p.value}</span>,
      },
      {
        field: "reason_code", headerName: "Reason code", width: 220, tooltipField: "message",
        cellClass: "mono reason", valueFormatter: (p) => blank(p.value),
      },
      { field: "order_id", headerName: "PayPal order", width: 180, cellClass: "mono", valueFormatter: (p) => blank(p.value) },
    ],
    [],
  );

  const rowClassRules = useMemo<RowClassRules<LedgerRow>>(() => ({ denied: (p) => p.data?.verdict === "DENY" }), []);
  const getRowId = useCallback((p: GetRowIdParams<LedgerRow>) => p.data.ledger_id, []);

  const allowed = rows.filter((r) => r.verdict === "ALLOW").length;
  return (
    <section className="pane ledger">
      <div className="pane-head">
        <h2>Ledger</h2>
        <span className="aside counts">
          {rows.length} attempts, {allowed} allowed, <span className="denied"><b>{rows.length - allowed} denied</b></span>
        </span>
      </div>
      <div className="grid">
        <AgGridReact<LedgerRow>
          theme={gridTheme}
          rowData={rows}
          columnDefs={columns}
          getRowId={getRowId}
          rowClassRules={rowClassRules}
          defaultColDef={{ sortable: true, filter: true, resizable: true }}
          animateRows={false}
          tooltipShowDelay={200}
          overlayNoRowsTemplate="No attempts yet."
        />
      </div>
    </section>
  );
}
