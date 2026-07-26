/* TechnicalEngine dashboard. Vanilla JS + lightweight-charts (vendored). */
"use strict";

const css = (name) => getComputedStyle(document.body).getPropertyValue(name).trim();
const COLORS = {
  up: css("--up"), down: css("--down"), accent: css("--accent"),
  ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"),
  surface: css("--surface"), page: css("--page"),
};
const $ = (id) => document.getElementById(id);
const fmtPct = (v) => v == null ? "–" : (v * 100).toFixed(1) + "%";
const fmtR = (v) => (v >= 0 ? "+" : "") + v.toFixed(1) + "R";

let state = { source: null, alerts: [], sigFilter: null, cumChart: null, candleChart: null };

const CHART_BASE = {
  // canvas logo off; attribution lives in the page footer instead
  layout: { background: { color: "transparent" }, textColor: COLORS.muted, attributionLogo: false,
            fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', fontSize: 11 },
  grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
  rightPriceScale: { borderColor: COLORS.grid },
  timeScale: { borderColor: COLORS.grid },
  crosshair: { horzLine: { labelBackgroundColor: COLORS.surface },
               vertLine: { labelBackgroundColor: COLORS.surface } },
};

async function getJSON(url) {
  const r = await fetch(url);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}

function showError(msg) {
  const el = $("err");
  el.textContent = msg;
  el.style.display = msg ? "block" : "none";
}

/* ---------------------------------------------------------------- sources */

async function init() {
  try {
    const meta = await getJSON("/api/meta");
    if (!meta.sources.length) {
      showError("No results yet. Run: python -m engine backtest (and/or --interval 1h), then refresh.");
      return;
    }
    const box = $("sources");
    for (const s of meta.sources) {
      const b = document.createElement("button");
      b.className = "pill";
      b.textContent = `${s.label} · ${s.alerts}`;
      b.onclick = () => setSource(s.key, b);
      b.dataset.key = s.key;
      box.appendChild(b);
    }
    setSource(meta.sources[0].key, box.firstChild);
  } catch (e) { showError(String(e.message || e)); }
}

async function setSource(key, btn) {
  state.source = key;
  state.sigFilter = null;
  document.querySelectorAll("#sources .pill").forEach(p => p.classList.toggle("on", p === btn));
  $("detail").style.display = "none";
  showError("");
  try {
    const [summary, alerts] = await Promise.all([
      getJSON(`/api/summary?source=${key}`),
      getJSON(`/api/alerts?source=${key}`),
    ]);
    state.alerts = alerts.alerts;
    renderSummary(summary);
    renderAlerts();
  } catch (e) { showError(String(e.message || e)); }
}

/* ---------------------------------------------------------------- summary */

function renderSummary(s) {
  const up = s.total_r >= 0;
  const num = $("totalR");
  num.textContent = fmtR(s.total_r);
  num.className = "num " + (up ? "up" : "down");
  $("heroSub").textContent =
    `cumulative R · ${s.label} · ${s.sessions} sessions · ${s.alerts} alerts (${s.per_day}/day)`;

  drawCumChart(s.cum_r, up);

  $("tiles").innerHTML = "";
  const tiles = [
    ["Win rate (decided)", s.win_lo === s.win_hi ? fmtPct(s.win_lo)
        : `${fmtPct(s.win_lo)}–${fmtPct(s.win_hi)}`, `${s.decided} decided`],
    ["Alerts", String(s.alerts), `${s.per_day}/day over ${s.sessions} sessions`],
    ["Ambiguous", String(s.ambiguous), "target and stop in the same bar"],
    ["Open at close", String(s.open), "neither target nor stop hit"],
    ["Unresolved", String(s.unresolved), "bars aged out of the store"],
  ];
  for (const [label, value, note] of tiles) {
    const d = document.createElement("div");
    d.className = "tile";
    d.innerHTML = `<div class="label"></div><div class="value"></div><div class="note"></div>`;
    d.children[0].textContent = label;
    d.children[1].textContent = value;
    d.children[2].textContent = note;
    $("tiles").appendChild(d);
  }

  renderStatsTable($("bysignal"), s.by_signal, s.by_score);
  renderStability($("stability"), s.stability_signal, s.stability_score, s.n_splits);
  $("stabNote").textContent = ` — win rate across ${s.n_splits} time slices (swing = last − first)`;
}

function drawCumChart(points, up) {
  $("cumchart").innerHTML = "";
  state.cumChart = LightweightCharts.createChart($("cumchart"), {
    ...CHART_BASE, height: 220, autoSize: true,
    rightPriceScale: { ...CHART_BASE.rightPriceScale, scaleMargins: { top: 0.15, bottom: 0.15 } },
    handleScroll: false, handleScale: false,
  });
  const color = up ? COLORS.up : COLORS.down;
  const series = state.cumChart.addAreaSeries({
    lineColor: color, lineWidth: 2,
    topColor: color + "26", bottomColor: color + "00",   // ~10% wash fading out
    priceLineVisible: false, lastValueVisible: true,
  });
  series.setData(points.map(p => ({ time: p.date, value: p.value })));
  state.cumChart.timeScale().fitContent();
}

function renderStatsTable(el, bySignal, byScore) {
  const cols = ["n", "decided", "target", "stopped", "ambiguous", "open", "exp_r", "win_lo", "win_hi"];
  const head = ["", ...cols];
  const fmt = (row, c) => (c === "win_lo" || c === "win_hi")
      ? (row.n >= 20 ? fmtPct(row[c]) : "n<20") : String(row[c] ?? "–");
  let html = "<thead><tr>" + head.map(h => `<th>${h.replace("_", " ")}</th>`).join("") + "</tr></thead><tbody>";
  for (const row of bySignal) html += "<tr><td>" + row.signal + "</td>" +
      cols.map(c => `<td>${fmt(row, c)}</td>`).join("") + "</tr>";
  for (const row of byScore) html += "<tr><td>score " + row.score + "</td>" +
      cols.map(c => `<td>${fmt(row, c)}</td>`).join("") + "</tr>";
  el.innerHTML = html + "</tbody>";
}

function renderStability(el, bySignal, byScore, n) {
  if (!bySignal.length && !byScore.length) { el.innerHTML = "<tbody><tr><td>Not enough sessions to split.</td></tr></tbody>"; return; }
  const labels = Array.from({ length: n }, (_, i) => `p${i + 1}`);
  let html = "<thead><tr><th></th>" +
      labels.map(l => `<th>n</th><th>win ${l}</th>`).join("") + "<th>swing</th></tr></thead><tbody>";
  const rowHtml = (name, row) => "<tr><td>" + name + "</td>" +
      labels.map(l => `<td>${row["n_" + l] ?? 0}</td><td>${fmtPct(row["win_" + l])}</td>`).join("") +
      `<td>${row.swing == null ? "–" : (row.swing >= 0 ? "+" : "") + (row.swing * 100).toFixed(1) + "%"}</td></tr>`;
  for (const row of bySignal) html += rowHtml(row.signal, row);
  for (const row of byScore) html += rowHtml("score " + row.score, row);
  el.innerHTML = html + "</tbody>";
}

/* ---------------------------------------------------------------- alerts */

function renderAlerts() {
  const signals = [...new Set(state.alerts.flatMap(a => a.signals.split(",")))].sort();
  const box = $("sigfilter");
  box.innerHTML = "";
  const mk = (label, val) => {
    const b = document.createElement("button");
    b.className = "pill" + ((state.sigFilter ?? null) === val ? " on" : "");
    b.textContent = label;
    b.onclick = () => { state.sigFilter = val; renderAlerts(); };
    box.appendChild(b);
  };
  mk("all", null);
  signals.forEach(s => mk(s, s));

  const rows = state.alerts.filter(a => !state.sigFilter || a.signals.split(",").includes(state.sigFilter));
  const tb = $("alerts").querySelector("tbody");
  tb.innerHTML = "";
  const OUTCOME = { target: "hit target", stop: "stopped", ambiguous: "both same bar",
                    open: "open", unresolved: "unresolved" };
  for (const a of rows.slice(0, 400)) {
    const t = new Date(a.ts);
    const tr = document.createElement("tr");
    const eod = a.fwd_eod == null ? "–" : ((a.fwd_eod >= 0 ? "+" : "") + (a.fwd_eod * 100).toFixed(2) + "%");
    tr.innerHTML =
      `<td>${a.ticker}</td><td>${a.session_date}</td>` +
      `<td>${t.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })}</td>` +
      `<td>${a.signals.replaceAll(",", " + ")}</td><td>${a.score}</td>` +
      `<td>${a.price.toFixed(2)}</td><td>${a.stop.toFixed(2)}</td><td>${a.target_1.toFixed(2)}</td>` +
      `<td><span class="badge"><span class="dot ${a.outcome}"></span>${OUTCOME[a.outcome] || a.outcome}</span></td>` +
      `<td>${eod}</td>`;
    tr.onclick = () => openDetail(a);
    tb.appendChild(tr);
  }
}

/* ---------------------------------------------------------------- detail */

async function openDetail(a) {
  try {
    const d = await getJSON(`/api/bars?source=${state.source}&ticker=${a.ticker}&date=${a.session_date}`);
    $("detail").style.display = "block";
    $("dTicker").textContent = a.ticker;
    $("dMeta").textContent =
      `${a.session_date} · ${a.signals.replaceAll(",", " + ")} · score ${a.score} · ` +
      `entry ${a.price.toFixed(2)} · stop ${a.stop.toFixed(2)} · T1 ${a.target_1.toFixed(2)} · T2 ${a.target_2.toFixed(2)}`;
    $("candles").innerHTML = "";
    const chart = LightweightCharts.createChart($("candles"), {
      ...CHART_BASE, height: 340, autoSize: true,
      timeScale: { ...CHART_BASE.timeScale, timeVisible: true, secondsVisible: false },
    });
    state.candleChart = chart;
    const candles = chart.addCandlestickSeries({
      upColor: COLORS.up, downColor: COLORS.down, borderVisible: false,
      wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    });
    candles.setData(d.bars);
    const vwap = chart.addLineSeries({ color: COLORS.accent, lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    vwap.setData(d.vwap);

    if (d.alert) {
      // bars are served on an ET-shifted epoch (see server.api_bars); the alert
      // marker must use the same axis or it lands hours away from its candle
      const alertBar = d.bars.reduce((best, b) =>
        Math.abs(b.time - d.alertTime) < Math.abs(best.time - d.alertTime) ? b : best, d.bars[0]);
      const mark = { time: alertBar.time,
                     position: "belowBar", color: COLORS.up, shape: "arrowUp", text: "BUY" };
      candles.setMarkers([mark]);
      const line = (price, title, color) => candles.createPriceLine({
        price, title, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true });
      line(d.alert.price, "entry", COLORS.ink2);
      line(d.alert.stop, "stop", COLORS.down);
      line(d.alert.target_1, "T1", COLORS.up);
      line(d.alert.target_2, "T2", COLORS.up);
    }
    // crosshair OHLC readout (the hover layer)
    chart.subscribeCrosshairMove(p => {
      const c = p.seriesData && p.seriesData.get(candles);
      $("ohlc").textContent = c
        ? `O ${c.open.toFixed(2)}  H ${c.high.toFixed(2)}  L ${c.low.toFixed(2)}  C ${c.close.toFixed(2)}`
        : "";
    });
    chart.timeScale().fitContent();
    $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) { showError(String(e.message || e)); }
}

init();
