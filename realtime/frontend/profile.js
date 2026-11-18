// Order modal: one tab for now, "Volume Profile". Loaded before app.js; exposes
// window.OrderModal. The profile is the WHOLE day as ai3's getsymprof builds it:
// OPEN, 10-minute continuous buckets (PM at the lunch reopen), CLOSE, each as a
// share of the day's total volume (see backend/profile.py).
(() => {
  const { useEffect, useMemo, useState } = React;
  const html = htm.bind(React.createElement);

  const pct = (v, d = 1) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
  const qty = (v) => (v == null ? "–" : Math.round(v).toLocaleString("en-US"));
  const compact = (v) => {
    if (v == null) return "–";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  };
  const clock = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

  // ------------------------------------------------------------------ chart
  // Two panels on one time axis (never one chart with two y-scales): the running
  // total on top, the share per bucket below. Geometry is in viewBox units; the
  // SVG scales to the modal's width.
  const W = 760, L = 48, R = 14;
  const TOP = { y0: 30, y1: 160 };        // cumulative panel
  const BOT = { y0: 214, y1: 344 };       // bucket panel
  const H = 370;

  function niceMax(v) {
    const steps = [0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1];
    for (const s of steps) if (v <= s) return s;
    return 1;
  }

  // a bar with its data end rounded (4px) and anchored to the baseline
  function barPath(x, w, yTop, yBase) {
    const r = Math.min(4, w / 2, yBase - yTop);
    if (r <= 0) return `M${x},${yBase}h${w}v0h${-w}z`;
    return `M${x},${yBase}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + w - r}` +
           `Q${x + w},${yTop} ${x + w},${yTop + r}V${yBase}Z`;
  }

  const AUCTION_NAME = { OPEN: "Open", PM: "PM", CLOSE: "Close" };

  // centred on the bar, but opening inward near either edge so it never leaves the chart
  function tipStyle(f) {
    const shift = f > 0.8 ? "-100%" : f < 0.2 ? "0%" : "-50%";
    return { left: `${f * 100}%`, transform: `translateX(${shift})` };
  }

  function ProfileChart({ p }) {
    const [hover, setHover] = useState(null);
    const rows = p.rows;
    const geo = useMemo(() => {
      // a margin either side so the OPEN / CLOSE bars and labels clear the axes
      const PAD = 12;   // minutes
      const tA = rows[0].start, tB = rows[rows.length - 1].end;
      const t0 = tA - PAD, t1 = tB + PAD;
      const x = (m) => L + ((m - t0) / (t1 - t0)) * (W - L - R);
      const yMax = niceMax(Math.max(...rows.map((r) => r.share)) * 1.08);
      const yc = (v) => TOP.y1 - v * (TOP.y1 - TOP.y0);
      const yb = (v) => BOT.y1 - (v / yMax) * (BOT.y1 - BOT.y0);
      // step-accurate running total: flat until a bucket starts, rising across it
      let line = `M${x(tA)},${yc(0)}`, prev = 0;
      for (const r of rows) line += `L${x(r.start)},${yc(prev)}L${x(r.end)},${yc(r.cum)}`, (prev = r.cum);
      const hours = [];
      for (let m = Math.ceil(t0 / 60) * 60; m <= t1; m += 60) hours.push(m);
      const bTicks = [0, yMax / 2, yMax];
      const bDec = bTicks.every((v) => Math.abs(v * 100 - Math.round(v * 100)) < 1e-9) ? 0 : 1;
      return { x, yc, yb, line, hours, bTicks, bDec, yMax };
    }, [rows]);
    const { x, yc, yb } = geo;
    const h = hover != null ? rows[hover] : null;

    return html`
      <div class="vp-chart">
        <div class="vp-legend">
          <span><i class="sw sw-cont"></i>Continuous, per 10 min</span>
          <span><i class="sw sw-auct"></i>Auctions (open${p.v_open_pm > 0 ? ", PM" : ""}, close)</span>
          <span><i class="sw sw-line"></i>Running total</span>
        </div>
        <svg viewBox=${`0 0 ${W} ${H}`} role="img"
             aria-label=${`Volume profile for ${p.sym}: share of the day's volume per bucket and running total`}
             onMouseLeave=${() => setHover(null)}>
          ${p.lunch ? html`
            <rect class="vp-lunch" x=${x(p.lunch[0])} y=${TOP.y0} width=${x(p.lunch[1]) - x(p.lunch[0])}
                  height=${BOT.y1 - TOP.y0} />
            <text class="vp-note" x=${(x(p.lunch[0]) + x(p.lunch[1])) / 2} y=${TOP.y0 + 12} text-anchor="middle">lunch</text>` : null}

          ${[0, 0.25, 0.5, 0.75, 1].map((v) => html`
            <line key=${"gc" + v} class="vp-grid" x1=${L} x2=${W - R} y1=${yc(v)} y2=${yc(v)} />
            <text key=${"tc" + v} class="vp-tick" x=${L - 6} y=${yc(v) + 4} text-anchor="end">${v * 100}%</text>`)}
          <text class="vp-panel" x=${L} y=${TOP.y0 - 14}>Running total, share of the day</text>
          <path class="vp-line" d=${geo.line} />

          ${geo.bTicks.map((v, i) => html`
            <line key=${"gb" + i} class=${i === 0 ? "vp-base" : "vp-grid"} x1=${L} x2=${W - R} y1=${yb(v)} y2=${yb(v)} />
            <text key=${"tb" + i} class="vp-tick" x=${L - 6} y=${yb(v) + 4} text-anchor="end">${pct(v, geo.bDec)}</text>`)}
          <text class="vp-panel" x=${L} y=${BOT.y0 - 14}>Share of the day per bucket</text>
          ${rows.map((r, i) => {
            const x0 = x(r.start) + 1, w = Math.max(1, x(r.end) - x(r.start) - 2);   // 2px gap
            const auction = r.phase !== "continuous";
            return html`<path key=${"b" + i} d=${barPath(x0, w, yb(r.share), yb(0))}
                              class=${"vp-bar " + (auction ? "auct" : "cont") + (hover === i ? " on" : "")} />`;
          })}
          ${rows.filter((r) => r.phase !== "continuous").map((r) => html`
            <text key=${"l" + r.label} class="vp-label" y=${yb(r.share) - 8}
                  x=${(x(r.start) + x(r.end)) / 2} text-anchor="middle">${AUCTION_NAME[r.label] || r.label}</text>`)}

          ${geo.hours.map((m) => html`
            <text key=${"x" + m} class="vp-tick" x=${x(m)} y=${H - 8} text-anchor="middle">${clock(m)}</text>`)}

          ${h ? html`<line class="vp-cross" x1=${x(h.end)} x2=${x(h.end)}
                           y1=${TOP.y0} y2=${BOT.y1} />
                     <circle class="vp-dot" cx=${x(h.end)} cy=${yc(h.cum)} r="4" />` : null}
          ${rows.map((r, i) => html`
            <rect key=${"h" + i} class="vp-hit" x=${x(r.start)} y=${TOP.y0} width=${Math.max(4, x(r.end) - x(r.start))}
                  height=${BOT.y1 - TOP.y0} onMouseEnter=${() => setHover(i)} />`)}
        </svg>
        ${h ? html`
          <div class="vp-tip" style=${tipStyle(x(h.end) / W)}>
            <b>${h.phase === "continuous" ? `${clock(h.start)}–${clock(h.end)}` : `${h.label} auction`}</b>
            <div>${pct(h.share, 2)} of the day · ${compact(h.share * p.vtot)} sh</div>
            <div class="muted">running total at ${clock(h.end)}: ${pct(h.cum, 1)}</div>
          </div>` : null}
      </div>`;
  }

  // ------------------------------------------------------------------ tab
  function ProfileTab({ sym }) {
    const [state, setState] = useState({ loading: true });
    useEffect(() => {
      let alive = true;
      setState({ loading: true });
      fetch(`/api/profile?sym=${encodeURIComponent(sym)}`)
        .then((r) => r.json())
        .then((d) => alive && setState(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
        .catch((e) => alive && setState({ error: String(e) }));
      return () => { alive = false; };
    }, [sym]);

    if (state.loading) return html`<div class="vp-msg">Loading volume profile…</div>`;
    if (state.error) return html`
      <div class="vp-msg err">No volume profile for ${sym}: ${state.error}
        ${state.source ? html`<div class="muted">source: ${state.source}</div>` : null}</div>`;

    const p = state.data;
    const auctions = p.rows.filter((r) => r.phase !== "continuous").reduce((a, r) => a + r.share, 0);
    const kpis = [
      ["ADV (continuous)", compact(p.adv), `${qty(p.adv)} shares`],
      ["Open auction", compact(p.v_open), `${qty(p.v_open)} shares · ${pct(p.v_open / p.vtot)} of the day`],
      ["Close auction", compact(p.v_close), `${qty(p.v_close)} shares · ${pct(p.v_close / p.vtot)} of the day`],
      ...(p.v_open_pm > 0 ? [["PM auction", compact(p.v_open_pm), `${qty(p.v_open_pm)} shares`]] : []),
      ["Auctions, share of day", pct(auctions), "open + PM + close over the day's total volume"],
    ];
    return html`
      <div>
        <div class="vp-kpis">
          ${kpis.map(([k, v, t]) => html`<div key=${k} class="vp-kpi" title=${t}><div class="k">${k}</div><div class="v">${v}</div></div>`)}
        </div>
        <${ProfileChart} p=${p} />
        <div class="vp-foot muted">
          Profile dated <b>${p.date}</b> · 
          Times on the region clock · ${p.source}
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ modal
  const TABS = [["profile", "Volume Profile"]];

  function OrderModal({ order, onClose }) {
    const [tab, setTab] = useState("profile");
    useEffect(() => {
      const onKey = (e) => e.key === "Escape" && onClose();
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    return html`
      <div class="modal-back" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
        <div class="modal" role="dialog" aria-modal="true" aria-label=${`Order ${order.sym}`}>
          <div class="modal-head">
            <h2>${order.sym} <span class=${"side-" + order.side}>${order.side}</span>
              ${order.strategy ? html`<span class="pill grey">${order.strategy}</span>` : null}</h2>
            <button class="modal-x" onClick=${onClose} aria-label="Close">×</button>
          </div>
          <div class="tabs" role="tablist">
            ${TABS.map(([k, label]) => html`
              <button key=${k} role="tab" aria-selected=${tab === k} class=${"tab" + (tab === k ? " on" : "")}
                      onClick=${() => setTab(k)}>${label}</button>`)}
          </div>
          <div class="modal-body">
            ${tab === "profile" ? html`<${ProfileTab} sym=${order.sym} />` : null}
          </div>
        </div>
      </div>`;
  }

  window.OrderModal = OrderModal;
})();
