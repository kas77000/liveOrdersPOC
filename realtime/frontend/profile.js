// Order modal, loaded before app.js; exposes window.OrderModal. Two tabs:
//
//   Full Day Volume Profile  the WHOLE day as ai3's getsymprof builds it: OPEN,
//                            10-minute continuous buckets (PM at the lunch reopen),
//                            CLOSE, each as a share of the day's total volume; with
//                            a tag-148 tilt, as if an order ran from open to close;
//                            and today's traded volume (qatt) with a forecast to
//                            the close (backend/today.py), each layer switchable.
//   Order Volume Profile     that curve cut to the order's window (editable start /
//                            end / auctions) and rescaled to 100% of the ORDER, as
//                            ai3's CustomProfile.toMap does for the live algo, with
//                            where the order should be now and where it is, and
//                            the tag-148 tilt (alpha, beta, mode) replayed as ai3's
//                            ProfileSnapshot does (front / back loading).
//
// The numbers come from backend/profile.py (build / order_profile / tilt_order_profile).
(() => {
  const { useEffect, useMemo, useRef, useState } = React;
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
    const steps = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8, 1];
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
  //   tilt     the tilt block when a tag-148 tilt is applied: rows then carry base_cum
  //            (the profile without it, drawn dashed) and tilt.breakpoint is marked
  //   today    /api/today (optional): today's traded shares per bucket (same rows as
  //            p) and the forecast to the close, drawn as a share of today's
  //            forecast total so its shape compares with the profile's
  //   show     { profile, tilt, today, forecast } - which layers are drawn
  //
  // Zoom: drag across either panel to zoom both on that time range, or pick a preset
  // (open, morning, afternoon, close). Zoomed, each panel's y-scale refits to what is
  // in view; double-click, Esc or "Reset zoom" goes back to the whole range. The zoom
  // is the chart's own state, so the 30s refresh of today's trades keeps it.
  const SHOW_ALL = { profile: true, tilt: true, today: true, forecast: true };
  const PAD = 12;          // minutes either side, so the OPEN / CLOSE bars clear the axes
  const MIN_ZOOM = 10;     // minutes: the narrowest zoom
  const DRAG_PX = 8;       // a drag shorter than this (viewBox units) is a click

  // ticks for a zoomed running-total axis: a round step, lo / hi on it
  function niceRange(lo, hi) {
    const steps = [0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25];
    const span = Math.max(hi - lo, 0.004);
    const step = steps.find((s) => span / s <= 5) || 0.25;
    const a = Math.max(0, Math.floor(lo / step + 1e-9) * step);
    let b = Math.ceil(hi / step - 1e-9) * step;
    if (b <= a) b = a + step;
    const ticks = [];
    for (let v = a; v <= b + 1e-9; v += step) ticks.push(+v.toFixed(6));
    return { lo: a, hi: b, ticks, dec: step < 0.01 ? 1 : 0 };
  }

  // quick ranges, clipped to the data; those too short or the whole range are dropped
  function zoomPresets(rows, lunch, fa, fb) {
    const cont = rows.filter((r) => r.phase === "continuous");
    const cA = cont.length ? cont[0].start : rows[0].start;
    const cB = cont.length ? cont[cont.length - 1].end : rows[rows.length - 1].end;
    const hasOpen = rows[0].phase !== "continuous", hasClose = rows[rows.length - 1].phase !== "continuous";
    const out = [[hasOpen ? "Open" : "First hour", fa, cA + 60]];
    if (lunch && lunch[0] > cA && lunch[1] < cB) out.push(["Morning", fa, lunch[0] + 5], ["Afternoon", lunch[1] - 5, fb]);
    out.push([hasClose ? "Close" : "Last hour", cB - 60, fb]);
    const whole = fb - fa;
    return out.map(([k, a, b]) => ({ key: k, a: Math.max(fa, a), b: Math.min(fb, b) }))
              .filter((z) => z.b - z.a >= 15 && z.b - z.a < 0.9 * whole);
  }

  function ProfileChart({ p, per, of, top, bot, marker, tilt, today, show = SHOW_ALL }) {
    const [hover, setHover] = useState(null);
    const [zoom, setZoom] = useState(null);       // { a, b, key } in minutes, or null
    const [drag, setDrag] = useState(null);       // { a, b } while dragging
    const svgRef = useRef(null);
    const dragRef = useRef(null);
    const clipId = useMemo(() => "vpc" + Math.random().toString(36).slice(2, 9), []);
    const tiltOn = !!(tilt && show.tilt);
    // with the tilt hidden, the profile is the market's own (base_*)
    const rows = useMemo(() => (tilt && !tiltOn
      ? p.rows.map((r) => ({ ...r, share: r.base_share, cum: r.base_cum })) : p.rows), [p.rows, tilt, tiltOn]);
    const td = today && today.rows && today.rows.length === rows.length ? today : null;
    const todayOn = !!(td && show.today);
    const fcOn = !!(td && show.forecast && !td.finished);
    const mainOn = show.profile || tiltOn;           // the profile (or tilted) line and bars
    const baseOn = tiltOn && show.profile;           // the market's line under the tilt
    const ghost = todayOn || fcOn;                   // profile bars as outlines behind today's

    const tA = rows[0].start, tB = rows[rows.length - 1].end;
    const fa = tA - PAD, fb = tB + PAD;
    // the zoom, clipped to the data (an edited order window may have moved away from it)
    const za = zoom ? Math.max(fa, zoom.a) : fa, zb = zoom ? Math.min(fb, zoom.b) : fb;
    const zoomed = !!zoom && zb - za >= MIN_ZOOM / 2;
    const t0 = zoomed ? za : fa, t1 = zoomed ? zb : fb;
    const presets = useMemo(() => zoomPresets(rows, p.lunch, fa, fb), [rows, p.lunch, fa, fb]);

    const geo = useMemo(() => {
      const x = (m) => L + ((m - t0) / (t1 - t0)) * (W - L - R);
      const inView = (r) => r.end > t0 && r.start < t1;
      // today, as a share of its forecast total: a (traded) + f (forecast) per bucket
      const T = td ? td.forecast_day || 1 : 1;
      const tr = td ? td.rows.map((r) => ({ a: r.actual / T, f: r.forecast / T, el: r.elapsed })) : [];
      // bucket panel: scaled to the bars in view
      const tops = [];
      rows.forEach((r, i) => {
        if (!inView(r)) return;
        if (mainOn) tops.push(r.share);
        if (todayOn || fcOn) tops.push((todayOn ? tr[i].a : 0) + (fcOn ? tr[i].f : 0));
      });
      const yMax = niceMax(Math.max(0.001, ...tops) * 1.08);
      // running-total panel: 0-100%, or zoomed, the range of the lines in view
      let cr = { lo: 0, hi: 1, ticks: [0, 0.25, 0.5, 0.75, 1], dec: 0 };
      if (zoomed) {
        const vals = [];
        let pc = 0, pb = 0, pa = 0, pf = 0;
        rows.forEach((r, i) => {
          const e = tr[i], ca = pa + (e ? e.a : 0), cf = pf + (e ? e.a + e.f : 0);
          if (inView(r)) {
            if (mainOn) vals.push(pc, r.cum);
            if (baseOn && r.base_cum != null) vals.push(pb, r.base_cum);
            if (todayOn && e && e.el > 0) vals.push(pa, ca);
            if (fcOn && e && e.el < 1) vals.push(pf + (e.el > 0 ? e.a : 0), cf);
          }
          pc = r.cum; pb = r.base_cum ?? 0; pa = ca; pf = cf;
        });
        if (marker && marker.t >= t0 && marker.t <= t1) vals.push(marker.expected, Math.min(1, marker.filled));
        if (vals.length) {
          let lo = Math.min(...vals), hi = Math.max(...vals);
          const m = Math.max((hi - lo) * 0.06, 0.002);
          cr = niceRange(Math.max(0, lo - m), Math.min(1, hi + m));
        }
      }
      const yc = (v) => TOP.y1 - ((v - cr.lo) / (cr.hi - cr.lo)) * (TOP.y1 - TOP.y0);
      const yb = (v) => BOT.y1 - (v / yMax) * (BOT.y1 - BOT.y0);
      // step-accurate running total: flat until a bucket starts, rising across it
      const stepLine = (key) => {
        let d = `M${x(tA)},${yc(0)}`, prev = 0;
        for (const r of rows) d += `L${x(r.start)},${yc(prev)}L${x(r.end)},${yc(r[key])}`, (prev = r[key]);
        return d;
      };
      const line = stepLine("cum");
      const baseLine = tilt && rows[0].base_cum != null ? stepLine("base_cum") : null;
      // today: traded up to now, then the forecast from now to the close
      let tLine = null, fLine = null, nowX = null, nowY = null;
      if (td) {
        const now = td.now_min;
        let d = `M${x(tA)},${yc(0)}`, cum = 0;
        for (let i = 0; i < rows.length; i++) {
          const r = rows[i], e = tr[i];
          if (e.el >= 1) d += `L${x(r.start)},${yc(cum)}L${x(r.end)},${yc(cum + e.a)}`;
          else if (e.el > 0) { d += `L${x(r.start)},${yc(cum)}L${x(now)},${yc(cum + e.a)}`; cum += e.a; break; }
          else { if (now > r.start) d += `L${x(now)},${yc(cum)}`; break; }
          cum += e.a;
        }
        tLine = d;
        nowX = x(Math.max(tA, Math.min(now, tB))); nowY = yc(td.traded / T);
        if (!td.finished) {
          let f = `M${nowX},${nowY}`, c2 = 0;
          for (let i = 0; i < rows.length; i++) {
            const r = rows[i], e = tr[i], from = c2;
            c2 += e.a + e.f;
            if (e.el >= 1) continue;
            f += `L${x(Math.max(r.start, now))},${yc(from + e.a)}L${x(r.end)},${yc(c2)}`;
          }
          fLine = f;
        }
      }
      const span = t1 - t0;
      const step = span > 300 ? 60 : span > 120 ? 30 : span > 60 ? 15 : span > 25 ? 10 : 5;
      const hours = [];
      for (let m = Math.ceil(t0 / step) * step; m <= t1; m += step) hours.push(m);
      const bTicks = [0, yMax / 2, yMax];
      const bDec = bTicks.every((v) => Math.abs(v * 100 - Math.round(v * 100)) < 1e-9) ? 0 : 1;
      return { x, yc, yb, line, baseLine, tLine, fLine, nowX, nowY, tr, T, hours, bTicks, bDec, cr, inView };
    }, [rows, tilt, td, mainOn, baseOn, todayOn, fcOn, t0, t1, zoomed, marker]);
    const { x, yc, yb, tr, inView } = geo;

    // viewBox x -> minute, from a mouse position
    const minuteAt = (clientX) => {
      const b = svgRef.current.getBoundingClientRect();
      const vx = ((clientX - b.left) / b.width) * W;
      const m = t0 + ((vx - L) / (W - L - R)) * (t1 - t0);
      return Math.max(t0, Math.min(t1, m));
    };
    const onMouseDown = (e) => {
      if (e.button !== 0) return;
      e.preventDefault();                          // no text selection while dragging
      const m = minuteAt(e.clientX);
      dragRef.current = { a: m, b: m };
      setDrag(dragRef.current);
    };
    const dragging = drag != null;
    useEffect(() => {
      if (!dragging) return;
      const move = (e) => { dragRef.current = { ...dragRef.current, b: minuteAt(e.clientX) }; setDrag(dragRef.current); };
      const up = () => {
        const d = dragRef.current;
        dragRef.current = null;
        setDrag(null);
        if (!d || Math.abs(x(d.b) - x(d.a)) < DRAG_PX) return;
        let a = Math.min(d.a, d.b), b = Math.max(d.a, d.b);
        if (b - a < MIN_ZOOM) { const c = (a + b) / 2; a = c - MIN_ZOOM / 2; b = c + MIN_ZOOM / 2; }
        setZoom({ a, b, key: null });
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
      return () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    }, [dragging, t0, t1]);
    // Esc zooms out first; the modal only closes on an Esc when not zoomed
    useEffect(() => {
      if (!zoomed) return;
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); setZoom(null); } };
      window.addEventListener("keydown", onKey, true);
      return () => window.removeEventListener("keydown", onKey, true);
    }, [zoomed]);

    const h = hover != null ? rows[hover] : null;
    const ht = h && td ? td.rows[hover] : null;
    const auctions = rows.filter((r) => r.phase !== "continuous").map((r) => (AUCTION_NAME[r.label] || r.label).toLowerCase());
    const lunchA = p.lunch ? Math.max(t0, p.lunch[0]) : 0, lunchB = p.lunch ? Math.min(t1, p.lunch[1]) : 0;
    const showLunch = p.lunch && lunchB > lunchA;
    const profName = td ? "Profile" : marker ? "Expected progress" : "Running total";
    const within = (t) => t != null && t >= t0 && t <= t1;
    const cx = (v) => Math.max(L, Math.min(W - R, v));     // an x clamped to the plot

    // the order now: where it should be (on the line) and where it is
    let mk = null;
    if (marker && within(marker.t)) {
      const mx = x(marker.t), right = mx < (L + W - R) / 2;
      const ye = yc(marker.expected), yf = yc(Math.min(1, marker.filled));
      // keep the two labels apart when the points are close
      const apart = Math.abs(ye - yf) < 14;
      mk = { mx, ye, yf, anchor: right ? "start" : "end", dx: right ? 9 : -9,
             yeL: apart ? Math.min(ye, yf) - 7 : ye + 4, yfL: apart ? Math.max(ye, yf) + 13 : yf + 4 };
    }
    const nowOn = td && (todayOn || fcOn) && !td.finished && within(td.now_min);
    const whole = of === "of the order" ? "Whole order" : "Whole day";
    const clipT = `url(#${clipId}-t)`, clipB = `url(#${clipId}-b)`;

    return html`
      <div>
        <div class="vp-zoom">
          <span class="muted">Zoom</span>
          <button class=${"vp-zbtn" + (zoomed ? "" : " on")} onClick=${() => setZoom(null)}>${whole}</button>
          ${presets.map((z) => html`
            <button key=${z.key} class=${"vp-zbtn" + (zoomed && zoom.key === z.key ? " on" : "")}
                    onClick=${() => setZoom(z)}>${z.key}</button>`)}
          ${zoomed ? html`<button class="vp-zbtn reset" onClick=${() => setZoom(null)} title="Esc or double-click the chart">Reset zoom</button>` : null}
          <span class="muted vp-zhint">${zoomed
            ? `${clock(t0)}–${clock(t1)} · double-click or Esc to zoom out`
            : "drag across the chart to zoom"}</span>
        </div>
      <div class="vp-chart">
        <div class="vp-legend">
          ${mainOn ? html`<span><i class=${"sw sw-cont" + (ghost ? " ghost" : "")}></i>${td ? "Profile, per 10 min" : "Continuous, per 10 min"}</span>` : null}
          ${mainOn && auctions.length ? html`<span><i class=${"sw sw-auct" + (ghost ? " ghost" : "")}></i>${td ? "Profile auctions" : `Auctions (${auctions.join(", ")})`}</span>` : null}
          ${mainOn ? html`<span><i class="sw sw-line"></i>${profName}${tiltOn ? " (tilted)" : ""}</span>` : null}
          ${baseOn && geo.baseLine ? html`<span><i class="sw sw-base"></i>${td ? "Profile without the tilt" : "Without the tilt"}</span>` : null}
          ${todayOn ? html`<span><i class="sw sw-today"></i>Traded today</span>` : null}
          ${fcOn ? html`<span><i class="sw sw-fc"></i>Forecast to the close</span>` : null}
          ${marker ? html`<span><i class="sw sw-fill"></i>Filled now</span>` : null}
        </div>
        <svg ref=${svgRef} viewBox=${`0 0 ${W} ${H}`} role="img" class=${dragging ? "dragging" : ""}
             aria-label=${`Volume profile for ${p.sym}: share ${of} per bucket and running total`}
             onMouseLeave=${() => setHover(null)} onMouseDown=${onMouseDown}
             onDoubleClick=${() => setZoom(null)}>
          <defs>
            <clipPath id=${clipId + "-t"}><rect x=${L} y=${TOP.y0 - 6} width=${W - L - R} height=${TOP.y1 - TOP.y0 + 8} /></clipPath>
            <clipPath id=${clipId + "-b"}><rect x=${L} y=${BOT.y0 - 22} width=${W - L - R} height=${BOT.y1 - BOT.y0 + 22} /></clipPath>
          </defs>
          ${showLunch ? html`
            <rect class="vp-lunch" x=${x(lunchA)} y=${TOP.y0} width=${x(lunchB) - x(lunchA)}
                  height=${BOT.y1 - TOP.y0} />
            <text class="vp-note" x=${(x(lunchA) + x(lunchB)) / 2} y=${TOP.y0 + 12} text-anchor="middle">lunch</text>` : null}

          ${geo.cr.ticks.map((v) => html`
            <line key=${"gc" + v} class="vp-grid" x1=${L} x2=${W - R} y1=${yc(v)} y2=${yc(v)} />
            <text key=${"tc" + v} class="vp-tick" x=${L - 6} y=${yc(v) + 4} text-anchor="end">${pct(v, geo.cr.dec)}</text>`)}
          <text class="vp-panel" x=${L} y=${TOP.y0 - 14}>${top}${zoomed && geo.cr.lo > 0 ? " · axis zoomed, not from 0%" : ""}</text>
          <g clipPath=${clipT}>
            ${baseOn && geo.baseLine ? html`<path class="vp-line base" d=${geo.baseLine} />` : null}
            ${mainOn ? html`<path class="vp-line" d=${geo.line} />` : null}
            ${fcOn && geo.fLine ? html`<path class="vp-fc-line" d=${geo.fLine} />` : null}
            ${todayOn ? html`<path class="vp-today-line" d=${geo.tLine} />` : null}
          </g>
          ${tiltOn && within(tilt.breakpoint) ? html`
            <line class="vp-bp" x1=${x(tilt.breakpoint)} x2=${x(tilt.breakpoint)} y1=${TOP.y0} y2=${TOP.y1} />
            <text class="vp-bpl" x=${x(tilt.breakpoint) + 4} y=${TOP.y1 - 4}>breakpoint ${clock(tilt.breakpoint)}</text>` : null}

          ${geo.bTicks.map((v, i) => html`
            <line key=${"gb" + i} class=${i === 0 ? "vp-base" : "vp-grid"} x1=${L} x2=${W - R} y1=${yb(v)} y2=${yb(v)} />
            <text key=${"tb" + i} class="vp-tick" x=${L - 6} y=${yb(v) + 4} text-anchor="end">${pct(v, geo.bDec)}</text>`)}
          <text class="vp-panel" x=${L} y=${BOT.y0 - 14}>${bot}${zoomed ? " · scaled to the bars in view" : ""}</text>
          <g clipPath=${clipB}>
            ${(todayOn || fcOn) ? rows.map((r, i) => {
              if (!inView(r)) return null;
              // today's bar: traded at the bottom, the forecast stacked on it
              const x0 = x(r.start) + 1, w = Math.max(1, x(r.end) - x(r.start) - 2), e = tr[i];
              const a = todayOn ? e.a : 0, f = fcOn ? e.f : 0;
              return html`<g key=${"t" + i} class=${hover === i ? "on" : ""}>
                ${a > 0 ? (f > 0 ? html`<rect class="vp-bar today" x=${x0} width=${w} y=${yb(a)} height=${yb(0) - yb(a)} />`
                                 : html`<path class="vp-bar today" d=${barPath(x0, w, yb(a), yb(0))} />`) : null}
                ${f > 0 ? html`<path class="vp-bar fc" d=${barPath(x0, w, yb(a + f), yb(a))} />` : null}
              </g>`;
            }) : null}
            ${mainOn ? rows.map((r, i) => {
              if (!inView(r)) return null;
              const x0 = x(r.start) + 1, w = Math.max(1, x(r.end) - x(r.start) - 2);   // 2px gap
              const auction = r.phase !== "continuous";
              return html`<path key=${"b" + i} d=${barPath(x0, w, yb(Math.max(0, r.share)), yb(0))}
                                class=${"vp-bar " + (auction ? "auct" : "cont") + (ghost ? " ghost" : "") + (hover === i ? " on" : "")} />`;
            }) : null}
            ${rows.filter((r) => r.phase !== "continuous" && inView(r)).map((r) => {
              const i = rows.indexOf(r), e = tr[i];
              const topv = Math.max(mainOn ? r.share : 0, e ? (todayOn ? e.a : 0) + (fcOn ? e.f : 0) : 0);
              return html`
              <text key=${"l" + r.label} class="vp-label" y=${yb(topv) - 8}
                    x=${cx((x(r.start) + x(r.end)) / 2)} text-anchor="middle">${AUCTION_NAME[r.label] || r.label}</text>`;
            })}
          </g>

          ${geo.hours.map((m) => html`
            <text key=${"x" + m} class="vp-tick" x=${x(m)} y=${H - 8} text-anchor="middle">${clock(m)}</text>`)}

          ${nowOn ? html`
            <line class="vp-now" x1=${geo.nowX} x2=${geo.nowX} y1=${TOP.y0} y2=${BOT.y1} />
            <text class="vp-note" x=${geo.nowX} y=${TOP.y0 - 3} text-anchor="middle">now ${clock(td.now_min)}</text>
            ${todayOn ? html`<circle class="vp-today-dot" cx=${geo.nowX} cy=${geo.nowY} r="4" />` : null}` : null}

          ${mk ? html`
            <line class="vp-now" x1=${mk.mx} x2=${mk.mx} y1=${TOP.y0} y2=${BOT.y1} />
            <text class="vp-note" x=${mk.mx} y=${TOP.y0 - 3} text-anchor="middle">now ${clock(marker.t)}</text>
            <circle class="vp-dot" cx=${mk.mx} cy=${mk.ye} r="4" />
            <path class="vp-fill" d=${`M${mk.mx},${mk.yf - 6}l6,6l-6,6l-6,-6z`} />
            <text class="vp-mlabel" x=${mk.mx + mk.dx} y=${mk.yeL} text-anchor=${mk.anchor}>Expected ${pct(marker.expected, 0)}</text>
            <text class="vp-mlabel" x=${mk.mx + mk.dx} y=${mk.yfL} text-anchor=${mk.anchor}>Filled ${pct(marker.filled, 0)}</text>` : null}

          ${h && !dragging ? html`<line class="vp-cross" x1=${cx(x(h.end))} x2=${cx(x(h.end))}
                           y1=${TOP.y0} y2=${BOT.y1} />
                     ${mainOn && within(h.end) ? html`<circle class="vp-dot" cx=${x(h.end)} cy=${yc(h.cum)} r="4" />` : null}` : null}
          ${drag ? html`<rect class="vp-brush" x=${Math.min(x(drag.a), x(drag.b))} y=${TOP.y0}
                              width=${Math.abs(x(drag.b) - x(drag.a))} height=${BOT.y1 - TOP.y0} />` : null}
          ${rows.map((r, i) => {
            if (!inView(r)) return null;
            const x0 = cx(x(r.start)), x1 = cx(x(r.end));
            return html`<rect key=${"h" + i} class="vp-hit" x=${x0} y=${TOP.y0} width=${Math.max(4, x1 - x0)}
                              height=${BOT.y1 - TOP.y0} onMouseEnter=${() => setHover(i)} />`;
          })}
        </svg>
        ${h && !dragging ? html`
          <div class="vp-tip" style=${tipStyle(cx(x(h.end)) / W)}>
            <b>${h.phase === "continuous" ? `${clock(h.start)}–${clock(h.end)}` : `${AUCTION_NAME[h.label] || h.label} auction`}</b>
            ${mainOn ? html`<div>${td ? "profile: " : ""}${pct(h.share, 2)} ${of} · ${compact(h.share * per)} sh</div>
            <div class="muted">running total at ${clock(h.end)}: ${pct(h.cum, 1)}${
              h.base_cum != null && tiltOn ? ` · without tilt ${pct(h.base_cum, 1)}` : ""}</div>` : null}
            ${ht && (todayOn || fcOn) ? html`
              ${todayOn ? html`<div>traded: ${compact(ht.actual)} sh${ht.trades ? ` · ${ht.trades} trades` : ""} · expected ${compact(ht.expected)}</div>` : null}
              ${fcOn && ht.forecast > 0 ? html`<div>forecast: ${compact(ht.forecast)} sh</div>` : null}
              <div class="muted">today ${pct((ht.actual + ht.forecast) / geo.T, 2)} of the day's ${td.finished ? "total" : "forecast"}</div>` : null}
          </div>` : null}
      </div>
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

  // tag 148 = "alpha,beta[,mode]" (9012 sub-tag); text fields so they can be typed freely
  const orderTilt = (o) => {
    const [a = "", b = "", m = "0"] = (o.tag148 || "").split(",").map((v) => v.trim());
    return { alpha: a, beta: b, mode: /^[0-3]$/.test(m) ? m : "0" };
  };
  const num = (s) => (s !== "" && Number.isFinite(Number(s)) ? Number(s) : null);
  const tiltReady = (t) => num(t.alpha) != null && num(t.beta) != null;
  // the engine tilts only with |alpha| > 1e-5 and 0 < beta < 1 (ProfileSnapshot ctor)
  function describeTilt(t) {
    const a = num(t.alpha), b = num(t.beta);
    if (a == null || b == null) return "no tilt";
    if (!(Math.abs(a) > 1e-5 && b > 0 && b < 1)) return "inactive (needs alpha ≠ 0 and 0 < beta < 1)";
    return a > 0 ? `front-load: ${pct(Math.abs(a), 0)} of the order done within the first ${pct(b, 0)} of the time`
                 : `back-load: ${pct(Math.abs(a), 0)} of the order kept for the last ${pct(b, 0)} of the time`;
  }
  const MODES = [["0", "0 · continuous only"], ["1", "1 · + auctions"], ["2", "2 · alpha × b"],
                 ["3", "3 · + auctions, alpha × b"]];

  // The tag-148 inputs, shared by both tabs: start from the order's own tag 148
  function TiltInputs({ tilt, setTilt, order, note, extra }) {
    return html`
      <div class="op-tilt">
        <b>Tilt · tag 148</b>
        <label title="alpha: share of the order moved; > 0 front-loads, < 0 back-loads">alpha
          <input class=${"op-num" + (tilt.alpha === "" || num(tilt.alpha) != null ? "" : " bad")} value=${tilt.alpha}
                 placeholder="–" onChange=${(e) => setTilt((t) => ({ ...t, alpha: e.target.value.trim() }))} /></label>
        <label title="beta: the part of the window's time it goes into (the first beta when front-loading, the last beta when back-loading)">beta
          <input class=${"op-num" + (tilt.beta === "" || num(tilt.beta) != null ? "" : " bad")} value=${tilt.beta}
                 placeholder="–" onChange=${(e) => setTilt((t) => ({ ...t, beta: e.target.value.trim() }))} /></label>
        <label title="mode: 1 / 3 also re-weight the auctions; 2 / 3 scale alpha by the profile's progress at the breakpoint">mode
          <select value=${tilt.mode} onChange=${(e) => setTilt((t) => ({ ...t, mode: e.target.value }))}>
            ${MODES.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}
          </select></label>
        <span>${describeTilt(tilt)}</span>
        <span class="muted">· order's tag 148: ${order.tag148 || "none"}</span>
        ${extra || null}
        ${note ? html`<span class="muted op-note">${note}</span>` : null}
      </div>`;
  }
  const tiltQuery = (q, t) => { if (tiltReady(t)) { q.set("alpha", num(t.alpha)); q.set("beta", num(t.beta)); q.set("mode", t.mode); } return q; };
  const tiltKpi = (tl) => tl ? [["Tilt breakpoint", `${clock(tl.breakpoint)} · ${pct(tl.b, 0)}`,
    `the profile has ${pct(tl.b, 1)} of the window's continuous volume by ${clock(tl.breakpoint)}` +
    (tl.mode > 1 ? ` · alpha scaled to ${tl.alpha_eff.toFixed(3)}` : "")]] : [];
  const TiltNotes = ({ t }) => t && t.notes.length
    ? html`<ul class="op-notes">${t.notes.map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>` : null;


  // ------------------------------------------------------------------ full day
  const TODAY_REFRESH_MS = 30000;
  function FullDayTab({ order }) {
    const sym = order.sym;
    const [tilt, setTilt] = useState(() => orderTilt(order));
    const [state, setState] = useState({ loading: true });
    const ready = tiltReady(tilt);
    const tiltEdited = JSON.stringify(tilt) !== JSON.stringify(orderTilt(order));
    // today's prints and the forecast, refreshed while the tab is open
    const [today, setToday] = useState({ loading: true });
    const [show, setShow] = useState(SHOW_ALL);
    useEffect(() => {
      let ac = null;
      const load = () => {
        if (ac) ac.abort();
        ac = new AbortController();
        getJSON(`/api/today?${new URLSearchParams({ sym })}`, ac.signal)
          .then((d) => setToday(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
          .catch((e) => e.name !== "AbortError" && setToday({ error: String(e) }));
      };
      load();
      const iv = setInterval(load, TODAY_REFRESH_MS);
      return () => { clearInterval(iv); if (ac) ac.abort(); };
    }, [sym]);
    useEffect(() => {
      const ac = new AbortController();
      const t = setTimeout(() => {
        const q = tiltQuery(new URLSearchParams({ sym }), tilt);
        getJSON(`/api/profile?${q}`, ac.signal)
          .then((d) => setState(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
          .catch((e) => e.name !== "AbortError" && setState({ error: String(e) }));
      }, 250);
      return () => { clearTimeout(t); ac.abort(); };
    }, [sym, ready, tilt.alpha, tilt.beta, tilt.mode]);

    const controls = html`
      <${TiltInputs} tilt=${tilt} setTilt=${setTilt} order=${order}
                     note="A what-if the engine never computes: its tilt applied as if an order ran from the open to the close, every auction included. The dashed line is the market's profile."
                     extra=${html`<button class="theme-btn" disabled=${!tiltEdited} onClick=${() => setTilt(orderTilt(order))}
                                          title="Back to the order's own tag 148">Reset to order</button>`} />`;

    if (state.loading) return html`<div>${controls}<div class="vp-msg">Loading volume profile…</div></div>`;
    if (state.error) return html`<div>${controls}<${Problem} sym=${sym} error=${state.error} source=${state.source} /></div>`;

    const p = state.data;
    const tl = p.tilt && p.tilt.active ? p.tilt : null;
    const td = today.data || null;
    const toggle = (k) => (e) => setShow((v) => ({ ...v, [k]: e.target.checked }));
    const layers = html`
      <div class="op-layers">
        <span class="muted">Show</span>
        <label><input type="checkbox" checked=${show.profile} onChange=${toggle("profile")} /> Volume profile</label>
        ${tl ? html`<label><input type="checkbox" checked=${show.tilt} onChange=${toggle("tilt")} /> Tilt (tag 148)</label>` : null}
        <label title=${td ? "" : "no trades loaded"}><input type="checkbox" checked=${show.today} disabled=${!td}
               onChange=${toggle("today")} /> Traded today</label>
        <label title=${td && td.finished ? "the close has traded: nothing left to forecast" : ""}>
          <input type="checkbox" checked=${show.forecast} disabled=${!td || td.finished}
                 onChange=${toggle("forecast")} /> Forecast to the close</label>
        ${today.error ? html`<span class="op-warn">Today's trades unavailable: ${today.error}</span>` : null}
        ${today.loading ? html`<span class="muted">loading today's trades…</span>` : null}
      </div>`;
    const todayKpis = td ? [
      ["Traded today", compact(td.traded), `${qty(td.traded)} shares in ${qty(td.trades)} trades, by ${clock(td.now_min)}`],
      ["Pace vs profile", td.pace_raw == null ? "–" : `${td.pace_raw.toFixed(2)}×`,
       `traded ÷ what the profile expected by now (${qty(td.expected_now)} sh); the forecast uses ` +
       `${td.pace.toFixed(2)}×, pulled towards 1 as if ${pct(td.prior_day_fraction, 0)} of a day had traded on profile`],
      [td.finished ? "Day's volume" : "Forecast day volume", compact(td.forecast_day),
       `${qty(td.forecast_day)} shares · the profile expects ${qty(td.expected_day)} (${pct(td.forecast_day / td.expected_day - 0, 0)} of it)`],
    ] : [];
    // the market's auction volume, untilted (the tilt only reshapes an order's schedule)
    const auctions = p.rows.filter((r) => r.phase !== "continuous").reduce((a, r) => a + (r.base_share ?? r.share), 0);
    return html`
      <div>
        ${controls}
        <${Kpis} items=${[
          ["ADV (continuous)", compact(p.adv), `${qty(p.adv)} shares`],
          ["Open auction", compact(p.v_open), `${qty(p.v_open)} shares · ${pct(p.v_open / p.vtot)} of the day`],
          ["Close auction", compact(p.v_close), `${qty(p.v_close)} shares · ${pct(p.v_close / p.vtot)} of the day`],
          ...(p.v_open_pm > 0 ? [["PM auction", compact(p.v_open_pm), `${qty(p.v_open_pm)} shares`]] : []),
          ["Auctions, share of day", pct(auctions), "open + PM + close over the day's total volume"],
          ...todayKpis,
          ...(show.tilt ? tiltKpi(tl) : []),
        ]} />
        ${layers}
        <${ProfileChart} p=${p} per=${p.vtot} of="of the day" tilt=${tl} today=${td} show=${show}
                         top=${tl && show.tilt ? "Running total with the tilt (an order from open to close), share of the day" : "Running total, share of the day"}
                         bot=${tl && show.tilt ? "Share of the day per bucket, tilted" : "Share of the day per bucket"} />
        ${show.tilt ? html`<${TiltNotes} t=${p.tilt} />` : null}
        <div class="vp-foot muted">Profile dated <b>${p.date}</b> · Times on the region clock · ${p.source}${
          td ? ` · today's trades: ${td.source}, refreshed every ${TODAY_REFRESH_MS / 1000}s` : ""}${
          td ? (td.finished ? " · today's bars and line are shares of the day's total"
                            : " · today's bars and line are shares of the day so far plus the forecast") : ""}</div>
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
    const [tilt, setTilt] = useState(() => orderTilt(order));
    const [state, setState] = useState({ loading: true });
    const edited = JSON.stringify(win) !== JSON.stringify(orderWindow(order));
    const tiltEdited = JSON.stringify(tilt) !== JSON.stringify(orderTilt(order));
    const ready = tiltReady(tilt);

    useEffect(() => {
      const ac = new AbortController();
      // short debounce: typing a time fires several changes
      const t = setTimeout(() => {
        const q = new URLSearchParams({ sym: order.sym, start: win.start, end: win.end,
                                        open: win.open ? 1 : 0, close: win.close ? 1 : 0 });
        tiltQuery(q, tilt);
        getJSON(`/api/order_profile?${q}`, ac.signal)
          .then((d) => setState(d.ok ? { data: d } : { error: d.error || "unknown error", source: d.source }))
          .catch((e) => e.name !== "AbortError" && setState({ error: String(e) }));
      }, 250);
      return () => { clearTimeout(t); ac.abort(); };
    }, [order.sym, win.start, win.end, win.open, win.close, ready, tilt.alpha, tilt.beta, tilt.mode]);

    const set = (k) => (e) => setWin((w) => ({ ...w, [k]: e.target.checked }));
    const setTime = (k) => (m) => setWin((w) => ({ ...w, [k]: m }));

    const controls = html`
      <div class="op-controls">
        <label>Start <${TimeField} value=${win.start} onCommit=${setTime("start")} /></label>
        <label>End <${TimeField} value=${win.end} onCommit=${setTime("end")} /></label>
        <label><input type="checkbox" checked=${win.open} onChange=${set("open")} /> Open auction</label>
        <label><input type="checkbox" checked=${win.close} onChange=${set("close")} /> Close auction</label>
        <button class="theme-btn" disabled=${!edited && !tiltEdited}
                onClick=${() => { setWin(orderWindow(order)); setTilt(orderTilt(order)); }}
                title="Back to the order's own start, end, auction flags and tag 148">Reset to order</button>
        ${edited || tiltEdited ? html`<span class="muted op-edited">edited — not the order's own settings</span>` : null}
      </div>
      <${TiltInputs} tilt=${tilt} setTilt=${setTilt} order=${order} />`;

    if (state.loading) return html`<div>${controls}<div class="vp-msg">Loading…</div></div>`;
    if (state.error) return html`<div>${controls}<${Problem} sym=${order.sym} error=${state.error} source=${state.source} /></div>`;

    const p = state.data;
    const tl = p.tilt && p.tilt.active ? p.tilt : null;
    const size = order.size || 0;
    const filled = size ? (order.filled || 0) / size : null;
    const now = p.now_min;
    // where the order should be now: the expected curve read at now (capped to the window)
    let marker = null, expectedNow = null;
    if (now >= p.start) {
      // capped to the window and to its last bucket (a window may run on into lunch)
      const t = Math.min(now, p.end, p.rows[p.rows.length - 1].end);
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
          ...tiltKpi(tl),
        ]} />
        <${ProfileChart} p=${p} per=${size} of="of the order" marker=${marker} tilt=${tl}
                         top=${tl ? "Expected progress with the tilt, share of the order" : "Expected progress, share of the order"}
                         bot="Share of the order per bucket" />
        <${TiltNotes} t=${p.tilt} />
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
            ${tab === "full" ? html`<${FullDayTab} order=${order} />` : null}
            ${tab === "order" ? html`<${OrderTab} order=${order} />` : null}
          </div>
        </div>
      </div>`;
  }

  window.OrderModal = OrderModal;
})();
