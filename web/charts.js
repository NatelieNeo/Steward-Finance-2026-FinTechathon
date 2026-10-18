// Dependency-free SVG charts (no CDN: the CSP only allows same-origin scripts).
import { esc, money, fmtDate } from './util.js';

export const PALETTE = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)', 'var(--c6)', 'var(--c7)', 'var(--c8)', 'var(--c9)'];

function niceStep(range, ticks = 4) {
  const raw = range / ticks;
  const mag = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

// Forecast: expected line, P10–P90 band, safety floor, scheduled events.
export function forecastChart(fc, { height = 230 } = {}) {
  const pts = fc.points || [];
  if (!pts.length) return '<div class="empty">No forecast yet.</div>';
  const W = 760, H = height, L = 64, R = 12, T = 12, B = 28;
  const all = pts.flatMap((p) => [p.low_cents, p.high_cents]).concat([fc.floor_cents, 0, fc.start_balance_cents]);
  let lo = Math.min(...all), hi = Math.max(...all);
  const step = niceStep(hi - lo);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
  let grid = '';
  for (let v = lo; v <= hi + 1; v += step) {
    grid += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`
      + `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(money(v, { whole: true }))}</text>`;
  }
  const band = pts.map((p, i) => `${x(i)},${y(p.high_cents)}`).join(' ') + ' '
    + pts.slice().reverse().map((p, j) => `${x(pts.length - 1 - j)},${y(p.low_cents)}`).join(' ');
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i)},${y(p.expected_cents)}`).join(' ');
  let events = '', ticks = '';
  pts.forEach((p, i) => {
    if (i % Math.ceil(pts.length / 6) === 0) ticks += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(fmtDate(p.date))}</text>`;
    (p.events || []).forEach((e) => {
      const cls = e.amount_cents >= 0 ? 'event-in' : 'event-out';
      events += `<circle class="${cls}" cx="${x(i)}" cy="${y(p.expected_cents)}" r="3.5"><title>${esc(fmtDate(p.date))}: ${esc(e.label)} ${esc(money(e.amount_cents, { sign: true }))}</title></circle>`;
    });
  });
  const floor = fc.floor_cents ? `<line class="floor" x1="${L}" x2="${W - R}" y1="${y(fc.floor_cents)}" y2="${y(fc.floor_cents)}"><title>Safety floor ${esc(money(fc.floor_cents))}</title></line>` : '';
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Cash-flow forecast">
    ${grid}<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>
    <polygon class="band" points="${band}"/>${floor}<path class="line" d="${line}"/>${events}${ticks}</svg>
    <div class="legend"><span><i style="background:var(--accent)"></i>Expected balance</span>
    <span><i style="background:color-mix(in srgb,var(--accent) 22%,transparent)"></i>P10–P90 range</span>
    <span><i style="background:var(--neg)"></i>Safety floor</span><span><i style="background:var(--pos)"></i>Income</span>
    <span><i style="background:var(--warn)"></i>Bills &amp; subscriptions</span></div>`;
}

// Stacked monthly bars by category.
export function stackedBars(months, { height = 240, top = 6 } = {}) {
  if (!months.length) return '<div class="empty">No data.</div>';
  const totals = {};
  months.forEach((m) => Object.entries(m.categories).forEach(([c, v]) => { totals[c] = (totals[c] || 0) + v; }));
  const cats = Object.entries(totals).sort((a, b) => b[1] - a[1]).map(([c]) => c);
  const shown = cats.slice(0, top);
  const W = 760, H = height, L = 64, R = 12, T = 10, B = 28;
  const maxV = Math.max(...months.map((m) => m.total_cents), 1);
  const step = niceStep(maxV);
  const hi = Math.ceil(maxV / step) * step;
  const bw = (W - L - R) / months.length;
  const y = (v) => T + (1 - v / hi) * (H - T - B);
  let svg = '';
  for (let v = 0; v <= hi; v += step) {
    svg += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(money(v, { whole: true }))}</text>`;
  }
  months.forEach((m, i) => {
    let acc = 0;
    const x0 = L + i * bw + bw * 0.18, w = bw * 0.64;
    const parts = shown.map((c) => [c, m.categories[c] || 0]);
    const other = m.total_cents - parts.reduce((s, [, v]) => s + v, 0);
    parts.push(['Other', other]);
    parts.forEach(([c, v], k) => {
      if (v <= 0) return;
      const y1 = y(acc + v), y0 = y(acc);
      const color = c === 'Other' ? 'var(--c9)' : PALETTE[k % PALETTE.length];
      svg += `<rect x="${x0}" y="${y1}" width="${w}" height="${Math.max(0, y0 - y1)}" fill="${color}" rx="2"><title>${esc(m.month)} · ${esc(c)}: ${esc(money(v))}</title></rect>`;
      acc += v;
    });
    svg += `<text x="${x0 + w / 2}" y="${H - 8}" text-anchor="middle">${esc(fmtDate(m.month + '-01', { month: 'short' }))}</text>`;
  });
  const legend = shown.map((c, k) => `<span><i style="background:${PALETTE[k % PALETTE.length]}"></i>${esc(c)}</span>`).join('')
    + '<span><i style="background:var(--c9)"></i>Other</span>';
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Monthly spending by category">${svg}</svg><div class="legend">${legend}</div>`;
}

// Donut of category totals.
export function donut(entries, { size = 180, label = '' } = {}) {
  const total = entries.reduce((s, [, v]) => s + Math.max(0, v), 0);
  if (!total) return '<div class="empty">No spending yet.</div>';
  const r = 70, c = 2 * Math.PI * r;
  let off = 0, arcs = '';
  entries.forEach(([name, v], i) => {
    const frac = Math.max(0, v) / total;
    arcs += `<circle r="${r}" cx="90" cy="90" fill="none" stroke="${PALETTE[i % PALETTE.length]}" stroke-width="26"
      stroke-dasharray="${frac * c} ${c}" stroke-dashoffset="${-off * c}" transform="rotate(-90 90 90)"><title>${esc(name)}: ${esc(money(v))} (${(frac * 100).toFixed(0)}%)</title></circle>`;
    off += frac;
  });
  const legend = entries.map(([n, v], i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(n)} · ${esc(money(v, { whole: true }))}</span>`).join('');
  return `<div class="row" style="gap:18px;align-items:center;flex-wrap:nowrap">
    <svg class="chart" viewBox="0 0 180 180" style="width:${size}px;flex:none" role="img" aria-label="Spending by category">${arcs}
      <text x="90" y="86" text-anchor="middle" style="font-size:12px">${esc(label)}</text>
      <text x="90" y="104" text-anchor="middle" style="font-size:15px;font-weight:650;fill:var(--text)">${esc(money(total, { whole: true }))}</text></svg>
    <div class="legend" style="flex-direction:column;gap:4px;margin:0">${legend}</div></div>`;
}

// Semicircle gauge 0–100.
export function gauge(score, grade) {
  const r = 52, c = Math.PI * r, frac = Math.max(0, Math.min(1, score / 100));
  const color = score >= 70 ? 'var(--pos)' : score >= 55 ? 'var(--warn)' : 'var(--neg)';
  return `<svg class="chart" viewBox="0 0 130 78" style="width:150px;flex:none" role="img" aria-label="Health score ${score}">
    <path d="M13 70 A52 52 0 0 1 117 70" fill="none" stroke="var(--border)" stroke-width="12" stroke-linecap="round"/>
    <path d="M13 70 A52 52 0 0 1 117 70" fill="none" stroke="${color}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${frac * c} ${c}"/>
    <text x="65" y="60" text-anchor="middle" style="font-size:24px;font-weight:700;fill:var(--text)">${score}</text>
    <text x="65" y="75" text-anchor="middle" style="font-size:10px">grade ${esc(grade)}</text></svg>`;
}

// Tiny sparkline for bill history.
export function sparkline(values, { w = 110, h = 28 } = {}) {
  if (!values || values.length < 2) return '';
  const lo = Math.min(...values), hi = Math.max(...values);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * (w - 4) + 2},${h - 3 - ((v - lo) / (hi - lo || 1)) * (h - 6)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/></svg>`;
}
