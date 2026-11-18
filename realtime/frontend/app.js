// Live Orders blotter. React 18 + htm (JSX-like templates, no build step).
const { memo, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } = React;

// Rendering thousands of <tr> on every keystroke/poll is what made typing lag;
// the table shows the top MAX_ROWS of the current sort/filter.
const MAX_ROWS = 500;
const html = htm.bind(React.createElement);

const DASH = html`<span class="dash">–</span>`;
const fmtNum = (v, d = 0) =>
  v == null ? DASH : v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtPct = (v) => (v == null ? DASH : `${+v.toFixed(2)}%`);
const txt = (v) => (v == null || v === "" ? DASH : v);

const FLOW_PILL = { Agency: "amber", Swap: "green" };

// [key, header, render, numeric?, advanced?]  advanced columns show only when
// "Show advanced information" is ticked.
const COLUMNS = [
  ["id_target", "ID", (r) => txt(r.id_target), true, true],
  ["sym", "Symbol", (r) => html`<b>${r.sym}</b>`],
  ["side", "Side", (r) => html`<span class=${"side-" + r.side}>${txt(r.side)}</span>`],
  ["limit", "Limit", (r) => (r.limit == null ? html`<span class="pill grey">MKT</span>` : fmtNum(r.limit, 2)), true],
  ["strategy", "Strategy", (r) => txt(r.strategy)],
  ["account", "Account", (r) => txt(r.account)],
  ["start", "Start Time", (r) => txt(r.start)],
  ["end", "End Time", (r) => txt(r.end)],
  ["market", "Market", (r) => txt(r.market)],
  ["min_pct", "Min %", (r) => fmtPct(r.min_pct), true],
  ["max_pct", "Max %", (r) => fmtPct(r.max_pct), true],
  ["target_pct", "Target %", (r) => fmtPct(r.target_pct), true],
  ["flow_type", "Flow Type", (r) => (r.flow_type ? html`<span class=${"pill " + (FLOW_PILL[r.flow_type] || "grey")}>${r.flow_type}</span>` : DASH)],
  ["client", "Client", (r) => txt(r.client)],
  ["pct_filled", "% Filled", (r) => r.pct_filled == null ? DASH : html`
      <span class="bar" title=${`${fmtNum(r.filled)} / ${fmtNum(r.size)}`}>
        <span class="track"><span class="fill" style=${{ width: Math.min(100, r.pct_filled) + "%" }}></span></span>
        <span class="pct">${r.pct_filled.toFixed(1)}%</span>
      </span>`, true],
  ["amount_usd", "Amount USD", (r) => (r.amount_usd == null ? DASH : "$" + fmtNum(r.amount_usd)), true],
];

function useLiveOrders() {
  const [rows, setRows] = useState({});
  const [status, setStatus] = useState({ ok: false, source: "connecting…" });
  const [changed, setChanged] = useState({});
  const [wsUp, setWsUp] = useState(false);

  useEffect(() => {
    let ws, retry, alive = true;
    const connect = () => {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      ws.onopen = () => setWsUp(true);
      ws.onclose = () => { setWsUp(false); if (alive) retry = setTimeout(connect, 2000); };
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.status) setStatus(m.status);
        if (m.type === "snapshot") setRows(Object.fromEntries(m.rows.map((r) => [r.key, r])));
        if (m.type === "delta") {
          setRows((prev) => {
            const next = { ...prev };
            m.removes.forEach((k) => delete next[k]);
            m.upserts.forEach((r) => (next[r.key] = r));
            return next;
          });
          const stamp = Date.now();
          setChanged((prev) => ({ ...prev, ...Object.fromEntries(m.upserts.map((r) => [r.key, stamp])) }));
        }
      };
    };
    connect();
    return () => { alive = false; clearTimeout(retry); ws && ws.close(); };
  }, []);

  return { rows, status, changed, wsUp };
}

function App() {
  const { rows, status, changed, wsUp } = useLiveOrders();
  const [sort, setSort] = useState({ key: "amount_usd", dir: -1 });
  const [query, setQuery] = useState("");
  const [hideDone, setHideDone] = useState(true);
  const [selected, setSelected] = useState(null);   // the order whose modal is open
  const closeModal = useCallback(() => setSelected(null), []);
  const [advanced, setAdvanced] = useState(() => {
    try { return localStorage.getItem("advanced") === "1"; } catch { return false; }
  });
  const toggleAdvanced = (on) => {
    setAdvanced(on);
    // don't stay sorted by a column that just disappeared
    if (!on) setSort((s) => (COLUMNS.find((c) => c[0] === s.key)?.[4] ? { key: "amount_usd", dir: -1 } : s));
    try { localStorage.setItem("advanced", on ? "1" : "0"); } catch {}
  };
  const [theme, setTheme] = useState(() => {
    try { const t = localStorage.getItem("theme"); if (t) return t; } catch {}
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const toggleTheme = () => {
    const t = theme === "dark" ? "light" : "dark";
    setTheme(t);
    try { localStorage.setItem("theme", t); } catch {}
  };
  const cols = useMemo(() => COLUMNS.filter((c) => advanced || !c[4]), [advanced]);

  // Deferred copies: the search box updates at once, the table catches up in the
  // background (and is interrupted by the next keystroke instead of blocking it).
  const dRows = useDeferredValue(rows);
  const dQuery = useDeferredValue(query);
  const dSort = useDeferredValue(sort);
  const dHideDone = useDeferredValue(hideDone);

  const list = useMemo(() => {
    const q = dQuery.trim().toLowerCase();
    let out = Object.values(dRows);
    if (dHideDone) out = out.filter((r) => !(r.pct_filled >= 100));
    if (q) out = out.filter((r) =>
      [r.sym, r.client, r.strategy, r.market, r.flow_type, r.account, String(r.id_target)]
        .some((v) => v && String(v).toLowerCase().includes(q)));
    const { key, dir } = dSort;
    return out.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null) return 1; if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [dRows, dSort, dQuery, dHideDone]);
  const shown = list.length > MAX_ROWS ? list.slice(0, MAX_ROWS) : list;


  const ok = wsUp && status.ok;
  const updated = status.updated ? new Date(status.updated * 1000).toLocaleTimeString() : "—";

  return html`
    <div>
    <div class="card">
      <div class="head">
        <h1>Active orders</h1>
        <span class="pill grey">${Object.keys(rows).length} orders</span>
        ${status.mode ? html`<span class=${"pill " + (status.mode === "realtime" ? "green" : "blue")}>
          ${status.mode === "realtime" ? "Real-time" : "Polling"}</span>` : null}
        <div class="spacer"></div>
        <input type="search" placeholder="Filter symbol, client, strategy…" value=${query}
               onInput=${(e) => setQuery(e.target.value)} />
        <label><input type="checkbox" checked=${hideDone} onChange=${(e) => setHideDone(e.target.checked)} />
          Hide fully filled</label>
        <label><input type="checkbox" checked=${advanced} onChange=${(e) => toggleAdvanced(e.target.checked)} />
          Show advanced information</label>
        <button class="theme-btn" onClick=${toggleTheme} title="Switch light / dark mode">
          ${theme === "dark" ? "☀ Light" : "☾ Dark"}</button>
        <div class="conn" title=${status.error || ""}>
          <span class=${"dot" + (ok ? " ok" : "")}></span>
          ${wsUp ? status.source : "disconnected"} · ${updated}
        </div>
      </div>
      ${status.error ? html`<div class="err">${status.error}</div>` : null}
      <div class="scroll">
        <table>
          <thead><tr>
            ${cols.map(([k, label, , num]) => html`
              <th key=${k} class=${num ? "num" : ""}
                  onClick=${() => setSort((s) => ({ key: k, dir: s.key === k ? -s.dir : -1 }))}>
                ${label}${sort.key === k ? (sort.dir < 0 ? " ▼" : " ▲") : ""}
              </th>`)}
          </tr></thead>
          <tbody>
            ${shown.map((r) => html`
              <${Row} key=${r.key} r=${r} cols=${cols} stamp=${changed[r.key]} onOpen=${setSelected} />`)}
          </tbody>
        </table>
        ${list.length === 0 ? html`<div class="empty">No orders.</div>` : null}
        ${list.length > MAX_ROWS ? html`<div class="empty">Showing ${MAX_ROWS} of ${list.length} orders — refine the filter or change the sort.</div>` : null}
      </div>
    </div>
    ${selected ? html`<${window.OrderModal} order=${selected} onClose=${closeModal} />` : null}
    </div>`;
}

// Re-keying the <tr> by the change stamp restarts the CSS flash animation.
// memo: rows whose data and stamp did not change are skipped on re-render (deltas
// keep the same object for unchanged rows).
const Row = memo(function Row({ r, cols, stamp, onOpen }) {
  return html`
    <tr key=${stamp || 0} class=${"clickable" + (stamp && Date.now() - stamp < 1500 ? " flash" : "")}
        onClick=${() => onOpen(r)} title="Open order details">
      ${cols.map(([k, , render, num]) => html`<td key=${k} class=${num ? "num" : ""}>${render(r)}</td>`)}
    </tr>`;
});

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
