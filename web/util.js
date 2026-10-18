// Shared helpers. Every value interpolated into HTML goes through esc(): merchant names and
// descriptions are third-party data and must never be rendered as markup.

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

let currency = 'SGD';
const SYMBOL = { SGD: 'S$', USD: 'US$', EUR: '€', GBP: '£', MYR: 'RM', AUD: 'A$' };
export const setCurrency = (c) => { currency = c || 'SGD'; };

export function money(cents, { sign = false, whole = false } = {}) {
  const n = Number(cents || 0) / 100;
  const s = Math.abs(n).toLocaleString('en-SG', {
    minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2,
  });
  const prefix = n < 0 ? '-' : sign && n > 0 ? '+' : '';
  return `${prefix}${SYMBOL[currency] || currency + ' '}${s}`;
}

export const pct = (x, digits = 0) => (x == null ? '—' : `${(x * 100).toFixed(digits)}%`);

export function fmtDate(iso, opts = { day: 'numeric', month: 'short' }) {
  if (!iso) return '—';
  const d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''));
  return d.toLocaleDateString('en-SG', opts);
}

export const titleCase = (s) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
export const label = (s) => String(s || '').replace(/_/g, ' ');

export function toCents(text) {
  const n = Number(String(text).replace(/[^0-9.]/g, ''));
  return Number.isFinite(n) ? Math.round(n * 100) : NaN;
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
