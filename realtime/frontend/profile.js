// Order modal, loaded before app.js; exposes window.OrderModal. Two tabs:
//
//   Full Day Volume Profile  the WHOLE day as ai3's getsymprof builds it: OPEN,
//                            10-minute continuous buckets (PM at the lunch reopen),
//                            CLOSE, each as a share of the day's total volume.
//   Order Volume Profile     that curve cut to the order's window (editable start /
//                            end / auctions) and rescaled to 100% of the ORDER, as
//                            ai3's CustomProfile.toMap does for the live algo, with
//                            where the order should be now and where it is.
//
// The numbers come from backend/profile.py (build / order_profile).
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
  const clock = (m) => {
    const t = Math.round(m);
    return `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
  };
  // "08:00:00" / "08:00" -> 480; null when absent
  const toMin = (s) => {
    if (!s) return null;
    const [h, m] = String(s).split(":").map(Number);
    return Number.isFinite(h) && Number.isFinite(m) ? h * 60 + m : null;
  };

  async function getJSON(url, signal) {
    const r = await fetch(url, { signal });
    return r.json();
  }

  // ------------------------------------------------------------------ chart
  // Two panels on one time axis (never one chart with two y-scales): the running
  // total on top, the share per bucket below. Geometry is in viewBox units; the
  // SVG scales to the modal's width.
  const W = 760, L = 48, R = 14;
  const TOP = { y0: 30, y1: 160 };        // running-total panel
  const BOT = { y0: 214, y1: 344 };       // bucket panel
  const H = 370;
  const AUCTION_NAME = { OPEN: "Open", PM: "PM", CLOSE: "Close" };

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

  // centred on a point, but opening inward near either edge so it never leaves the chart
  function tipStyle(f) {
    const shift = f > 0.8 ? "-100%" : f < 0.2 ? "0%" : "-50%";
    return { left: `${f * 100}%`, transform: `translateX(${shift})` };
  }

  // the running total at minute t: whole bars before t, plus the part of the
  // continuous bar t cuts (an auction counts once its bar has ended)
  function cumAt(rows, t) {
    let tot = 0;
    for (const r of rows) {
      if (r.end <= t) tot += r.share;
      else if (r.start < t && r.phase === "continuous") tot += r.share * (t - r.start) / (r.end - r.start);
    }
    return tot;
  }

  // ProfileChart props:
  //   p        rows (start, end, label, phase, share, cum) + lunch
  //   per      shares per 1.0 of share (the day's volume, or the order size)
  //   of       "of the day" | "of the order"
  //   top/bot  panel titles
  //   marker   { t, expected, filled } - the order now (optional)
  function ProfileChart({ p, per, of, top, bot, marker }) {
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
      const span = t1 - t0, step = span > 300 ? 60 : span > 120 ? 30 : 15;
      const hours = [];
      for (let m = Math.ceil(t0 / step) * step; m <= t1; m += step) hours.push(m);
      const bTicks = [0, yMax / 2, yMax];
      const bDec = bTicks.every((v) => Math.abs(v * 100 - Math.round(v * 100)) < 1e-9) ? 0 : 1;
      return { x, yc, yb, line, hours, bTicks, bDec, t0, t1 };
    }, [rows]);
    const { x, yc, yb } = geo;
    const h = hover != null ? rows[hover] : null;
    const auctions = rows.filter((r) => r.phase !== "continuous").map((r) => (AUCTION_NAME[r.label] || r.label).toLowerCase());
    const showLunch = p.lunch && p.lunch[0] >= geo.t0 && p.lunch[1] <= geo.t1;

    // the order now: where it should be (on the line) and where it is
    let mk = null;
    if (marker) {
      const mx = x(marker.t), right = mx < (L + W - R) / 2;
      const ye = yc(marker.expected), yf = yc(Math.min(1, marker.filled));
      // keep the two labels apart when the points are close
      const apart = Math.abs(ye - yf) < 14;
      mk = { mx, ye, yf, anchor: right ? "start" : "end", dx: right ? 9 : -9,
             yeL: apart ? Math.min(ye, yf) - 7 : ye + 4, yfL: apart ? Math.max(ye, yf) + 13 : yf + 4 };
    }

    return html`
      <div class="vp-chart">
        <div class="vp-legend">
          <span><i class="sw sw-cont"></i>Continuous, per 10 min</span>
          ${auctions.length ? html`<span><i class="sw sw-auct"></i>Auctions (${auctions.join(", ")})</span>` : null}
          <span><i class="sw sw-line"></i>${marker ? "Expected progress" : "Running total"}</span>
          ${marker ? html`<span><i class="sw sw-fill"></i>Filled now</span>` : null}
        </div>
        <svg viewBox=${`0 0 ${W} ${H}`} role="img"
             aria-label=${`Volume profile for ${p.sym}: share ${of} per bucket and running total`}
             onMouseLeave=${() => setHover(null)}>
          ${showLunch ? html`
            <rect class="vp-lunch" x=${x(p.lunch[0])} y=${TOP.y0} width=${x(p.lunch[1]) - x(p.lunch[0])}
                  height=${BOT.y1 - TOP.y0} />
            <text class="vp-note" x=${(x(p.lunch[0]) + x(p.lunch[1])) / 2} y=${TOP.y0 + 12} text-anchor="middle">lunch</text>` : null}

          ${[0, 0.25, 0.5, 0.75, 1].map((v) => html`
            <line key=${"gc" + v} class="vp-grid" x1=${L} x2=${W - R} y1=${yc(v)} y2=${yc(v)} />
            <text key=${"tc" + v} class="vp-tick" x=${L - 6} y=${yc(v) + 4} text-anchor="end">${v * 100}%</text>`)}
          <text class="vp-panel" x=${L} y=${TOP.y0 - 14}>${top}</text>
          <path class="vp-line" d=${geo.line} />

          ${geo.bTicks.map((v, i) => html`
            <line key=${"gb" + i} class=${i === 0 ? "vp-base" : "vp-grid"} x1=${L} x2=${W - R} y1=${yb(v)} y2=${yb(v)} />
            <text key=${"tb" + i} class="vp-tick" x=${L - 6} y=${yb(v) + 4} text-anchor="end">${pct(v, geo.bDec)}</text>`)}
          <text class="vp-panel" x=${L} y=${BOT.y0 - 14}>${bot}</text>
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

          ${mk ? html`
            <line class="vp-now" x1=${mk.mx} x2=${mk.mx} y1=${TOP.y0} y2=${BOT.y1} />
            <text class="vp-note" x=${mk.mx} y=${TOP.y0 - 3} text-anchor="middle">now ${clock(marker.t)}</text>
            <circle class="vp-dot" cx=${mk.mx} cy=${mk.ye} r="4" />
            <path class="vp-fill" d=${`M${mk.mx},${mk.yf - 6}l6,6l-6,6l-6,-6z`} />
            <text class="vp-mlabel" x=${mk.mx + mk.dx} y=${mk.yeL} text-anchor=${mk.anchor}>Expected ${pct(marker.expected, 0)}</text>
            <text class="vp-mlabel" x=${mk.mx + mk.dx} y=${mk.yfL} text-anchor=${mk.anchor}>Filled ${pct(marker.filled, 0)}</text>` : null}

          ${h ? html`<line class="vp-cross" x1=${x(h.end)} x2=${x(h.end)}
                           y1=${TOP.y0} y2=${BOT.y1} />
                     <circle class="vp-dot" cx=${x(h.end)} cy=${yc(h.cum)} r="4" />` : null}
          ${rows.map((r, i) => html`
            <rect key=${"h" + i} class="vp-hit" x=${x(r.start)} y=${TOP.y0} width=${Math.max(4, x(r.end) - x(r.start))}
                  height=${BOT.y1 - TOP.y0} onMouseEnter=${() => setHover(i)} />`)}
        </svg>
        ${h ? html`
          <div class="vp-tip" style=${tipStyle(x(h.end) / W)}>
            <b>${h.phase === "continuous" ? `${clock(h.start)}–${clock(h.end)}` : `${AUCTION_NAME[h.label] || h.label} auction`}</b>
            <div>${pct(h.share, 2)} ${of} · ${compact(h.share * per)} sh</div>
            <div class="muted">running total at ${clock(h.end)}: ${pct(h.cum, 1)}</div>
          </div>` : null}
      </div>`;
  }

  function Kpis({ items }) {
    return html`
      <div class="vp-kpis">
        ${items.map(([k, v, t]) => html`<div key=${k} class="vp-kpi" title=${t}><div class="k">${k}</div><div class="v">${v}</div></div>`)}
      </div>`;
  }

  function Problem({ sym, error, source }) {
    return html`
      <div class="vp-msg err">No volume profile for ${sym}: ${error}
        ${source ? html`<div class="muted">source: ${source}</div>` : null}</div>`;
  }

  // ------------------------------------------------------------------ full day
  function FullDayTab({ sym }) {
    const [state, setState] = useState({ loading: true });
    useEffect(() => {
      const ac = new AbortController();
      setState({ loading: true });
      getJSON(`/api/profile?sym=${encodeURIComponent(sym)}`, ac.signal)
        .then((d) => setState(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
        .catch((e) => e.name !== "AbortError" && setState({ error: String(e) }));
      return () => ac.abort();
    }, [sym]);

    if (state.loading) return html`<div class="vp-msg">Loading volume profile…</div>`;
    if (state.error) return html`<${Problem} sym=${sym} error=${state.error} source=${state.source} />`;

    const p = state.data;
    const auctions = p.rows.filter((r) => r.phase !== "continuous").reduce((a, r) => a + r.share, 0);
    return html`
      <div>
        <${Kpis} items=${[
          ["ADV (continuous)", compact(p.adv), `${qty(p.adv)} shares`],
          ["Open auction", compact(p.v_open), `${qty(p.v_open)} shares · ${pct(p.v_open / p.vtot)} of the day`],
          ["Close auction", compact(p.v_close), `${qty(p.v_close)} shares · ${pct(p.v_close / p.vtot)} of the day`],
          ...(p.v_open_pm > 0 ? [["PM auction", compact(p.v_open_pm), `${qty(p.v_open_pm)} shares`]] : []),
          ["Auctions, share of day", pct(auctions), "open + PM + close over the day's total volume"],
        ]} />
        <${ProfileChart} p=${p} per=${p.vtot} of="of the day"
                         top="Running total, share of the day" bot="Share of the day per bucket" />
        <div class="vp-foot muted">Profile dated <b>${p.date}</b> · Times on the region clock · ${p.source}</div>
      </div>`;
  }

  // ------------------------------------------------------------------ order window
  const orderWindow = (o) => ({
    start: toMin(o.start) ?? 0,              // no start -> from midnight
    end: toMin(o.end) ?? 24 * 60 - 1,        // no end   -> to the end of the day
    open: o.doopen !== false,                 // null -> assume yes (as core-vp)
    close: o.doclose !== false,
  });
  const toInput = (m) => clock(Math.max(0, Math.min(m, 24 * 60 - 1)));

  // A 24-hour HH:MM field on the region clock. <input type=time> follows the OS locale
  // (12h on some machines) while every other time here is 24h, so this is plain text:
  // a valid time is applied as it is typed; anything else waits and shows in red.
  function TimeField({ value, onCommit }) {
    const [text, setText] = useState(toInput(value));
    useEffect(() => setText(toInput(value)), [value]);
    const valid = /^([01]?\d|2[0-3]):[0-5]\d$/.test(text);
    return html`<input class=${"op-time" + (valid ? "" : " bad")} value=${text} maxlength="5" inputmode="numeric"
                       placeholder="HH:MM" aria-invalid=${!valid}
                       onChange=${(e) => { const t = e.target.value; setText(t);
                                           if (/^([01]?\d|2[0-3]):[0-5]\d$/.test(t)) onCommit(toMin(t)); }}
                       onBlur=${() => setText(toInput(value))} />`;
  }

  function OrderTab({ order }) {
    // The window starts as the order's own and can be edited; "Reset" restores it.
    const [win, setWin] = useState(() => orderWindow(order));
    const [state, setState] = useState({ loading: true });
    const edited = JSON.stringify(win) !== JSON.stringify(orderWindow(order));

    useEffect(() => {
      const ac = new AbortController();
      // short debounce: typing a time fires several changes
      const t = setTimeout(() => {
        const q = new URLSearchParams({ sym: order.sym, start: win.start, end: win.end,
                                        open: win.open ? 1 : 0, close: win.close ? 1 : 0 });
        getJSON(`/api/order_profile?${q}`, ac.signal)
          .then((d) => setState(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
          .catch((e) => e.name !== "AbortError" && setState({ error: String(e) }));
      }, 250);
      return () => { clearTimeout(t); ac.abort(); };
    }, [order.sym, win.start, win.end, win.open, win.close]);

    const set = (k) => (e) => setWin((w) => ({ ...w, [k]: e.target.checked }));
    const setTime = (k) => (m) => setWin((w) => ({ ...w, [k]: m }));

    const controls = html`
      <div class="op-controls">
        <label>Start <${TimeField} value=${win.start} onCommit=${setTime("start")} /></label>
        <label>End <${TimeField} value=${win.end} onCommit=${setTime("end")} /></label>
        <label><input type="checkbox" checked=${win.open} onChange=${set("open")} /> Open auction</label>
        <label><input type="checkbox" checked=${win.close} onChange=${set("close")} /> Close auction</label>
        <button class="theme-btn" disabled=${!edited} onClick=${() => setWin(orderWindow(order))}
                title="Back to the order's own start, end and auction flags">Reset to order</button>
        ${edited ? html`<span class="muted op-edited">edited — not the order's own window</span>` : null}
      </div>`;

    if (state.loading) return html`<div>${controls}<div class="vp-msg">Loading…</div></div>`;
    if (state.error) return html`<div>${controls}<${Problem} sym=${order.sym} error=${state.error} source=${state.source} /></div>`;

    const p = state.data;
    const size = order.size || 0;
    const filled = size ? (order.filled || 0) / size : null;
    const now = p.now_min;
    // where the order should be now: the expected curve read at now (capped to the window)
    let marker = null, expectedNow = null;
    if (now >= p.start) {
      const t = Math.min(now, p.end);
      expectedNow = cumAt(p.rows, t);
      if (filled != null) marker = { t, expected: expectedNow, filled };
    }
    const gap = marker ? marker.filled - marker.expected : null;
    const span = `${clock(p.start)} → ${clock(p.end)}`;
    const aucs = [p.open_included && "open", p.rows.some((r) => r.phase === "pm auction") && "PM",
                  p.close_included && "close"].filter(Boolean).join(" + ") || "none";

    return html`
      <div>
        ${controls}
        <${Kpis} items=${[
          ["Window", span, `auctions included: ${aucs}`],
          ["Share of the day in window", pct(p.v12), "ai3 v12: cp(end) − cp(start) of the day's volume"],
          ["Expected market volume", compact(p.expected_volume), `${qty(p.expected_volume)} shares in the window`],
          ["Order / expected volume", p.expected_volume ? pct(size / p.expected_volume) : "–",
           `order size ${qty(size)} over the expected market volume: the participation it implies`],
          ["Expected by now · filled", expectedNow == null ? "not started" : `${pct(expectedNow, 0)} · ${pct(filled, 0)}`,
           gap == null ? "the window has not started yet" : `${gap >= 0 ? "ahead" : "behind"} by ${pct(Math.abs(gap), 1)} of the order`],
        ]} />
        <${ProfileChart} p=${p} per=${size} of="of the order" marker=${marker}
                         top="Expected progress, share of the order" bot="Share of the order per bucket" />
        <div class="vp-foot muted">
          Auctions included: ${aucs} · Profile dated <b>${p.date}</b> · Times on the region clock · ${p.source}
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ modal
  const TABS = [["full", "Full Day Volume Profile"], ["order", "Order Volume Profile"]];

  function OrderModal({ order, onClose }) {
    const [tab, setTab] = useState("full");
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
            ${tab === "full" ? html`<${FullDayTab} sym=${order.sym} />` : null}
            ${tab === "order" ? html`<${OrderTab} order=${order} />` : null}
          </div>
        </div>
      </div>`;
  }

  window.OrderModal = OrderModal;
})();
