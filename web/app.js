// Steward single-page app. No framework, no inline handlers (strict CSP): all clicks go
// through one delegated listener keyed by data-act attributes.
import { esc, money, pct, fmtDate, label, titleCase, toCents, setCurrency, debounce } from './util.js';
import { forecastChart, stackedBars, donut, gauge, sparkline } from './charts.js';

const S = { me: null, csrf: null, convo: null, chat: [], txPage: 0, txFilter: { q: '', category: '' }, pending: 0, meta: null };
const $app = document.getElementById('app');

// ------------------------------------------------------------------ API
async function api(path, { method = 'GET', body, form, retry = true } = {}) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') opts.headers['X-CSRF-Token'] = S.csrf || '';
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  if (form) opts.body = form;
  const res = await fetch('/api/v1' + path, opts);
  const isJson = (res.headers.get('content-type') || '').includes('application/json');
  const data = isJson ? await res.json() : null;
  if (!res.ok) {
    const err = (data && data.error) || { code: `http_${res.status}`, message: res.statusText };
    if (err.code === 'step_up_required' && retry) {
      if (await stepUp()) return api(path, { method, body, form, retry: false });
    }
    if (err.code === 'not_authenticated' && S.me) { S.me = null; renderAuth(); }
    throw Object.assign(new Error(err.message || 'Request failed'), { code: err.code, status: res.status });
  }
  return data;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3500);
}
const fail = (e) => toast(e.message || String(e), 'error');

// Step-up: re-enter the password for sensitive changes (raising tier/limits, big approvals).
function stepUp() {
  const dlg = document.getElementById('modal');
  dlg.innerHTML = `<form class="modal-body" method="dialog" id="stepup-form">
    <h2>Confirm it's you</h2>
    <p class="muted">This change gives the agent more power or moves a larger amount, so Steward asks for your password again.</p>
    <label class="field">Password<input type="password" name="password" autocomplete="current-password" required></label>
    <div class="row" style="justify-content:flex-end"><button class="btn" value="cancel" type="button" data-close>Cancel</button>
    <button class="btn primary" type="submit">Confirm</button></div></form>`;
  return new Promise((resolve) => {
    const form = dlg.querySelector('form');
    dlg.querySelector('[data-close]').addEventListener('click', () => { dlg.close(); resolve(false); });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      try {
        await api('/auth/step-up', { method: 'POST', body: { password: form.password.value }, retry: false });
        dlg.close(); resolve(true);
      } catch (e) { fail(e); }
    });
    dlg.showModal();
    form.password.focus();
  });
}

// ------------------------------------------------------------------ auth
function renderAuth(mode = 'register') {
  const demoEmail = `demo-${Math.random().toString(36).slice(2, 8)}@steward.demo`;
  $app.className = '';
  $app.innerHTML = `<div class="auth">
    <section class="auth-hero">
      <div class="row"><img src="/static/favicon.svg" alt="" width="40" height="40"><b style="font-size:20px">Steward</b></div>
      <h1>An AI that budgets, audits your bills and acts on your money, only as far as you allow.</h1>
      <ul>
        <li>Smart budgets that learn from your real spending and warn you before you overspend.</li>
        <li>Bill analysis that catches spikes, sneaky new charges, GST errors and late fees.</li>
        <li>Spending insights: duplicate charges, price hikes, overlapping subscriptions, cash-flow forecasts.</li>
        <li>Every action passes a deterministic policy engine and lands in a tamper-evident audit log.</li>
      </ul>
      <div class="tiers"><div><b>Tier 0 · Observer</b>Read-only insights</div><div><b>Tier 1 · Organizer</b>Budgets &amp; categories</div>
      <div><b>Tier 2 · Assistant</b>Prepares payments for approval</div><div><b>Tier 3 · Autopilot</b>Routine moves inside limits</div></div>
      <p class="small" style="opacity:.75">Prototype on a simulated bank. No real money moves.</p>
    </section>
    <section class="auth-panel"><div class="card auth-card">
      <div class="tabs"><button data-act="auth-tab" data-mode="register" class="${mode === 'register' ? 'active' : ''}">Create account</button>
      <button data-act="auth-tab" data-mode="login" class="${mode === 'login' ? 'active' : ''}">Sign in</button></div>
      ${mode === 'register' ? `<form id="register-form" class="stack">
        <label class="field">Name<input name="display_name" value="Alex" required maxlength="80"></label>
        <label class="field">Email<input name="email" type="email" value="${esc(demoEmail)}" required></label>
        <label class="field">Password<input name="password" type="password" autocomplete="new-password" required minlength="10" placeholder="10+ characters, letters and digits"></label>
        <label class="check"><input type="checkbox" name="seed_demo" checked> Load 6 months of sandbox banking data</label>
        <button class="btn primary" type="submit">Create account</button>
        <p class="small muted">You will need this password again to raise the agent's permissions.</p></form>`
      : `<form id="login-form" class="stack">
        <label class="field">Email<input name="email" type="email" required autocomplete="username"></label>
        <label class="field">Password<input name="password" type="password" required autocomplete="current-password"></label>
        <button class="btn primary" type="submit">Sign in</button></form>`}
    </div></section></div>`;
  const reg = document.getElementById('register-form');
  if (reg) reg.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = reg.querySelector('button[type=submit]'); btn.disabled = true; btn.textContent = 'Setting up your sandbox bank…';
    try {
      const me = await api('/auth/register', { method: 'POST', body: {
        display_name: reg.display_name.value, email: reg.email.value, password: reg.password.value, seed_demo: reg.seed_demo.checked } });
      onSignedIn(me);
    } catch (e) { fail(e); btn.disabled = false; btn.textContent = 'Create account'; }
  });
  const login = document.getElementById('login-form');
  if (login) login.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try { onSignedIn(await api('/auth/login', { method: 'POST', body: { email: login.email.value, password: login.password.value } })); }
    catch (e) { fail(e); }
  });
}

function onSignedIn(me) {
  S.me = me.user; S.csrf = me.csrf_token; setCurrency(me.user.currency);
  if (location.hash !== '#/dashboard' && (!location.hash || location.hash === '#/login')) {
    location.hash = '#/dashboard'; // hashchange triggers render()
  } else {
    render();
  }
}

// ------------------------------------------------------------------ shell
const NAV = [
  ['dashboard', 'Dashboard'], ['spending', 'Spending'], ['transactions', 'Transactions'], ['bills', 'Bills & subscriptions'],
  ['budgets', 'Budgets & goals'], ['agent', 'Agent'], ['audit', 'Audit trail'], ['sandbox', 'Sandbox'],
];

function shell(page, inner) {
  return `<div class="shell"><aside class="sidebar">
    <div class="brand"><img src="/static/favicon.svg" alt=""><div>Steward<small>AI money co-pilot</small></div></div>
    <nav class="nav">${NAV.map(([k, t]) => `<a href="#/${k}" class="${k === page ? 'active' : ''}">${esc(t)}${k === 'agent' && S.pending ? `<span class="count">${S.pending}</span>` : ''}</a>`).join('')}</nav>
    <div class="sidebar-foot">${esc(S.meta ? `AI: ${S.meta.llm_provider === 'anthropic' ? 'Claude (' + S.meta.llm_model + ')' : 'offline rules'}` : '')}</div>
  </aside><div class="main"><header class="topbar">
    <span class="chip">Sandbox date · ${esc(fmtDate(S.me.sandbox_date, { day: 'numeric', month: 'short', year: 'numeric' }))}</span>
    <span id="tier-chip"></span><div class="spacer"></div>
    <span class="muted">${esc(S.me.display_name)}</span><button class="btn sm" data-act="logout">Sign out</button>
  </header><main class="content" id="page">${inner}</main></div></div>`;
}

async function refreshChrome() {
  try {
    const [pol, pending] = await Promise.all([api('/agent/policy'), api('/agent/actions?status=pending_approval')]);
    S.pending = pending.length;
    const p = pol.policy, t = pol.tiers[p.tier];
    const chip = document.getElementById('tier-chip');
    if (chip) chip.innerHTML = p.frozen ? '<span class="chip bad">Agent frozen</span>' : `<span class="chip tier">Tier ${p.tier} · ${esc(t.name)}</span>`;
    const link = document.querySelector('.nav a[href="#/agent"]');
    if (link) link.innerHTML = `Agent${S.pending ? `<span class="count">${S.pending}</span>` : ''}`;
  } catch { /* chrome is best-effort */ }
}

const PAGES = {};
async function render() {
  if (!S.me) {
    try { const me = await api('/auth/me'); S.me = me.user; S.csrf = me.csrf_token; setCurrency(me.user.currency); }
    catch { renderAuth(); return; }
  }
  if (!S.meta) { try { S.meta = await api('/meta'); } catch { S.meta = null; } }
  const page = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
  const fn = PAGES[page] || PAGES.dashboard;
  $app.className = '';
  $app.innerHTML = shell(page in PAGES ? page : 'dashboard', '<div class="empty">Loading…</div>');
  try { document.getElementById('page').innerHTML = await fn(); }
  catch (e) { if (S.me) document.getElementById('page').innerHTML = `<div class="callout bad">${esc(e.message)}</div>`; }
  refreshChrome();
  if (page === 'agent') wireChat();
  if (page === 'transactions') wireTransactions();
}
const rerender = () => render();

// ------------------------------------------------------------------ shared fragments
function insightItem(i) {
  const act = i.suggested_action;
  return `<div class="item"><span class="dot ${esc(i.severity)}"></span><div class="grow">
    <h4>${esc(i.title)}</h4><p>${esc(i.body)}</p>
    ${act ? `<div class="row" style="margin-top:8px"><button class="btn sm" data-act="insight-action" data-payload="${esc(JSON.stringify(act))}">Ask Steward: ${esc(act.label)}</button></div>` : ''}
  </div></div>`;
}

function billCard(b, { compact = false } = {}) {
  const findings = b.audit.findings || [];
  return `<div class="action-card"><div class="row between"><div><b>${esc(b.payee)}</b> <span class="muted small">${esc(b.period)}</span></div>
    <div class="row"><span class="chip ${esc(b.status)}">${esc(label(b.status))}</span><span class="chip ${esc(b.audit.status)}">audit: ${esc(label(b.audit.status))}</span></div></div>
    <div class="row between"><span class="num" style="font-size:18px;font-weight:650">${esc(money(b.amount_cents))}</span>
    <span class="muted small">due ${esc(fmtDate(b.due_on))} · ${b.days_until_due >= 0 ? `${b.days_until_due} day(s) left` : `${-b.days_until_due} day(s) overdue`}${b.agent_allowlisted ? ' · agent-allowlisted' : ''}</span></div>
    ${findings.length ? `<ul class="reasons">${findings.map((f) => `<li><b>${esc(f.title)}</b> ${compact ? '' : '— ' + esc(f.detail)}</li>`).join('')}</ul>` : '<span class="small pos">Audit clean: totals, GST and line items check out.</span>'}
    ${compact ? '' : `<details><summary>Line items</summary><table>${b.audit.line_items.map((li) => `<tr><td>${esc(li.label)}</td><td><span class="chip">${esc(li.type)}</span></td><td class="r num">${esc(money(li.amount_cents))}</td></tr>`).join('')}</table></details>`}
    ${b.status === 'paid' ? '' : `<div class="row"><button class="btn sm primary" data-act="agent-pay" data-id="${b.id}">Ask Steward to pay</button><button class="btn sm" data-act="pay-myself" data-id="${b.id}">Pay it myself</button></div>`}
  </div>`;
}

function actionCard(a, { buttons = true } = {}) {
  const checks = (a.checks || []).map((c) => `<span class="${c.passed ? 'pos' : c.on_fail === 'deny' ? 'neg' : ''}">${c.passed ? '✓' : c.on_fail === 'deny' ? '✕' : '!'} ${esc(label(c.name))}</span><span class="muted">${c.passed ? '' : esc(c.detail)}</span>`).join('');
  return `<div class="action-card"><div class="row between"><b>${esc(a.summary || a.action_type)}</b><span class="chip ${esc(a.status)}">${esc(label(a.status))}</span></div>
    <div class="row small muted"><span class="chip">${esc(a.risk)}</span><span>${esc(label(a.action_type))}</span><span>· via ${esc(a.origin)}</span><span>· ${esc(fmtDate(a.created_on))}</span>${a.requires_step_up ? '<span class="chip warn">needs password</span>' : ''}</div>
    ${a.rationale ? `<p class="small">${esc(a.rationale)}</p>` : ''}
    ${a.reasons && a.reasons.length ? `<ul class="reasons">${a.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
    ${a.result && a.result.error ? `<div class="callout bad small">${esc(a.result.error)}</div>` : ''}
    ${checks ? `<details><summary>Policy checks (${(a.checks || []).length})</summary><div class="checks">${checks}</div></details>` : ''}
    ${buttons && a.status === 'pending_approval' ? `<div class="row"><button class="btn sm primary" data-act="approve" data-id="${a.id}">Approve</button><button class="btn sm danger" data-act="reject" data-id="${a.id}">Reject</button></div>` : ''}
  </div>`;
}

// ------------------------------------------------------------------ dashboard
PAGES.dashboard = async () => {
  const o = await api('/overview');
  const checking = o.accounts.find((a) => a.type === 'checking') || { balance_cents: 0 };
  const savings = o.accounts.filter((a) => a.type === 'savings').reduce((s, a) => s + a.balance_cents, 0);
  const comps = Object.values(o.health.components);
  return `<div class="page-head"><div><h1>Good to see you, ${esc(S.me.display_name)}</h1>
    <p class="muted">Here's where your money stands on ${esc(fmtDate(o.today, { weekday: 'long', day: 'numeric', month: 'long' }))}.</p></div>
    <div class="row"><button class="btn" data-act="run-cycle">Run agent check-up</button><a class="btn primary" href="#/agent">Ask Steward</a></div></div>
  <div class="grid g5">
    <div class="card kpi"><div class="label">Everyday balance</div><div class="value">${esc(money(checking.balance_cents))}</div><div class="sub">${esc(checking.name || '')}</div></div>
    <div class="card kpi"><div class="label">Savings</div><div class="value">${esc(money(savings))}</div><div class="sub">across savings accounts</div></div>
    <div class="card kpi"><div class="label">Spent this month</div><div class="value">${esc(money(o.month.spent_cents))}</div><div class="sub">income ${esc(money(o.month.income_cents))}</div></div>
    <div class="card kpi"><div class="label">Safe to spend</div><div class="value">${esc(money(o.safe_to_spend_cents))}</div><div class="sub">until payday ${esc(fmtDate(o.next_payday))}</div></div>
    <div class="card kpi"><div class="label">Agent</div><div class="value" style="font-size:18px">${o.agent.frozen ? '<span class="neg">Frozen</span>' : `Tier ${o.agent.tier} · ${esc(o.agent.name)}`}</div><div class="sub"><a href="#/agent">${S.pending || 0} awaiting approval</a></div></div>
  </div>
  <div class="grid g-main" style="margin-top:16px">
    <div class="card"><div class="card-head"><h2>30-day cash-flow forecast</h2><span class="muted">P(overdraft) ${esc(pct(o.forecast.overdraft_probability, 1))} · low point ${esc(money(o.forecast.min_expected_cents))} on ${esc(fmtDate(o.forecast.min_expected_date))}</span></div>${forecastChart(o.forecast)}</div>
    <div class="card"><div class="card-head"><h2>Financial health</h2></div><div class="gauge-wrap">${gauge(o.health.score, o.health.grade)}
      <div class="health-comps">${comps.map((c) => `<span title="${esc(c.explain)}">${esc(c.label)}</span><b class="num">${Math.round(c.score)}</b>`).join('')}</div></div>
      <div class="stack" style="margin-top:12px">${o.health.focus.map((f) => `<div class="callout small">${esc(f)}</div>`).join('')}</div></div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><div class="card-head"><h2>Insights</h2><a href="#/spending" class="small">All insights →</a></div><div class="list">${o.insights.map(insightItem).join('') || '<div class="empty">Nothing needs your attention.</div>'}</div></div>
    <div class="stack">
      <div class="card"><div class="card-head"><h2>Bills to pay</h2><a href="#/bills" class="small">Bills →</a></div><div class="stack">${o.open_bills.map((b) => billCard(b, { compact: true })).join('') || '<div class="empty">No open bills.</div>'}</div></div>
      <div class="card"><div class="card-head"><h2>Alerts</h2></div><div class="list">${o.alerts.map((a) => `<div class="item"><span class="dot ${esc(a.severity)}"></span><div class="grow"><h4>${esc(a.title)}</h4><p>${esc(a.body)}</p></div><button class="btn sm" data-act="read-alert" data-id="${a.id}">Dismiss</button></div>`).join('') || '<div class="empty">No new alerts.</div>'}</div></div>
    </div>
  </div>`;
};

// ------------------------------------------------------------------ spending
PAGES.spending = async () => {
  const [sp, ins] = await Promise.all([api('/spending?months=6'), api('/insights')]);
  const cur = sp.months[sp.months.length - 1];
  const entries = Object.entries(cur.categories).sort((a, b) => b[1] - a[1]);
  const top = entries.slice(0, 7);
  const rest = entries.slice(7).reduce((s, [, v]) => s + v, 0);
  if (rest) top.push(['Everything else', rest]);
  return `<div class="page-head"><div><h1>Spending insights</h1><p class="muted">Where the money goes, what changed, and what Steward noticed.</p></div>
    <button class="btn" data-act="digest">Write my weekly digest</button></div>
  <div id="digest"></div>
  <div class="grid g-wide">
    <div class="card"><div class="card-head"><h2>Last 6 months</h2><span class="muted">bars = spending, excludes transfers</span></div>${stackedBars(sp.months)}</div>
    <div class="card"><div class="card-head"><h2>This month by category</h2></div>${donut(top, { label: 'month to date' })}</div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><div class="card-head"><h2>All insights</h2><span class="muted">${ins.length}</span></div><div class="list">${ins.map(insightItem).join('')}</div></div>
    <div class="stack">
      <div class="card"><div class="card-head"><h2>Top merchants this month</h2></div><table>${sp.top_merchants_this_month.map((m) => `<tr><td>${esc(m.merchant)}</td><td class="r num">${esc(money(m.spent_cents))}</td></tr>`).join('')}</table></div>
      <div class="card"><div class="card-head"><h2>vs. same point last month</h2></div><table><tr><th>Category</th><th class="r">This month</th><th class="r">Last month</th><th class="r">Change</th></tr>
      ${sp.mom_changes.map((c) => `<tr><td>${esc(c.category)}</td><td class="r num">${esc(money(c.current_cents))}</td><td class="r num">${esc(money(c.previous_cents))}</td><td class="r num ${c.delta_cents > 0 ? 'neg' : 'pos'}">${esc(money(c.delta_cents, { sign: true }))}</td></tr>`).join('')}</table></div>
      <div class="card"><div class="card-head"><h2>Monthly totals</h2></div><table><tr><th>Month</th><th class="r">Income</th><th class="r">Spending</th><th class="r">Kept</th></tr>
      ${sp.months.map((m) => `<tr><td>${esc(fmtDate(m.month + '-01', { month: 'long', year: 'numeric' }))}</td><td class="r num">${esc(money(m.income_cents))}</td><td class="r num">${esc(money(m.total_cents))}</td><td class="r num ${m.income_cents - m.total_cents >= 0 ? 'pos' : 'neg'}">${esc(money(m.income_cents - m.total_cents, { sign: true }))}</td></tr>`).join('')}</table></div>
    </div>
  </div>`;
};

// ------------------------------------------------------------------ transactions
PAGES.transactions = async () => {
  const f = S.txFilter;
  const qs = new URLSearchParams({ limit: 50, offset: S.txPage * 50 });
  if (f.q) qs.set('q', f.q);
  if (f.category) qs.set('category', f.category);
  const [tx, accounts] = await Promise.all([api('/transactions?' + qs), api('/accounts')]);
  const opts = (sel) => tx.categories.map((c) => `<option ${c === sel ? 'selected' : ''}>${esc(c)}</option>`).join('');
  const pages = Math.max(1, Math.ceil(tx.total / 50));
  return `<div class="page-head"><div><h1>Transactions</h1><p class="muted">${tx.total} transactions. Change a category and Steward learns it for that merchant.</p></div>
    <a class="btn" href="/api/v1/transactions/export.csv">Export CSV</a></div>
  <div class="grid g-tx">
  <div class="card"><div class="row" style="margin-bottom:12px">
    <input id="tx-q" placeholder="Search merchant or description" value="${esc(f.q)}" style="flex:1">
    <select id="tx-cat"><option value="">All categories</option>${tx.categories.map((c) => `<option ${c === f.category ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></div>
    <div class="table-wrap"><table><tr><th>Date</th><th>Merchant</th><th>Category</th><th class="r">Amount</th></tr>
    ${tx.items.map((t) => `<tr><td class="muted num">${esc(fmtDate(t.date))}</td>
      <td><div>${esc(t.merchant)} ${t.flags.includes('prompt_injection_suspected') ? '<span class="chip bad" title="This description contained instructions aimed at AI assistants and is quarantined from the agent.">quarantined</span>' : ''}</div><div class="small muted">${esc(t.description)}</div></td>
      <td><select data-recat="${t.id}">${opts(t.category)}</select><div class="small muted">${esc(t.category_source)}${t.category_source === 'model' ? ' · ' + pct(t.confidence) : ''}</div></td>
      <td class="r num ${t.amount_cents < 0 ? '' : 'pos'}">${esc(money(t.amount_cents, { sign: t.amount_cents > 0 }))}</td></tr>`).join('')}</table></div>
    <div class="row between" style="margin-top:12px"><span class="muted small">Page ${S.txPage + 1} of ${pages}</span>
    <div class="row"><button class="btn sm" data-act="tx-page" data-d="-1" ${S.txPage === 0 ? 'disabled' : ''}>Previous</button><button class="btn sm" data-act="tx-page" data-d="1" ${S.txPage + 1 >= pages ? 'disabled' : ''}>Next</button></div></div></div>
  <div class="card"><h2>Import a bank statement</h2><p class="muted small" style="margin:6px 0 12px">CSV with Date, Description and Amount (or Debit/Credit) columns. Duplicates are skipped; suspicious descriptions are quarantined.</p>
    <form id="import-form" class="stack"><label class="field">Account<select name="account_id">${accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select></label>
    <input type="file" name="file" accept=".csv,text/csv" required><button class="btn primary" type="submit">Import</button></form><div id="import-result" style="margin-top:10px"></div></div>
  </div>`;
};

function wireTransactions() {
  const q = document.getElementById('tx-q');
  const cat = document.getElementById('tx-cat');
  if (q) q.addEventListener('input', debounce(() => { S.txFilter.q = q.value; S.txPage = 0; rerender(); }, 450));
  if (cat) cat.addEventListener('change', () => { S.txFilter.category = cat.value; S.txPage = 0; rerender(); });
  document.querySelectorAll('select[data-recat]').forEach((sel) => sel.addEventListener('change', async () => {
    try { const r = await api(`/transactions/${sel.dataset.recat}`, { method: 'PATCH', body: { category: sel.value, remember: true } });
      toast(`Saved. Applied to ${r.updated} transaction(s) from this merchant.`); }
    catch (e) { fail(e); }
  }));
  const form = document.getElementById('import-form');
  if (form) form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    try {
      const r = await api('/transactions/import', { method: 'POST', form: fd });
      document.getElementById('import-result').innerHTML = `<div class="callout ok">Imported ${r.imported}, skipped ${r.duplicates_skipped} duplicate(s), quarantined ${r.quarantined}.${r.warnings.length ? '<br>' + r.warnings.map(esc).join('<br>') : ''}</div>`;
    } catch (e) { fail(e); }
  });
}

// ------------------------------------------------------------------ bills
const SAMPLE_BILL = `Singtel Mobile - Statement Oct 2026
5G SIM Only 100GB plan            42.11
Premium SMS service (VAS)          4.91
Roaming data - Malaysia           15.00
Late payment charge               10.00
GST 9%                             6.90
Total amount due              S$ 78.92`;

PAGES.bills = async () => {
  const [bills, rec, payees] = await Promise.all([api('/bills'), api('/recurring'), api('/payees')]);
  const subs = rec.series.filter((s) => s.kind === 'subscription');
  const billsR = rec.series.filter((s) => s.kind === 'bill');
  const income = rec.series.filter((s) => s.kind === 'income');
  const histRow = (s) => `<tr><td><b>${esc(s.merchant)}</b><div class="small muted">${esc(s.category)} · ${esc(s.cadence)} · ${s.occurrences}×</div></td>
    <td>${sparkline(s.history.map((h) => h.amount_cents))}</td>
    <td class="r num">${esc(money(s.next_expected_amount_cents))}${s.price_change_pct ? `<div class="small ${s.price_change_pct > 0 ? 'neg' : 'pos'}">${s.price_change_pct > 0 ? '▲' : '▼'} ${esc(pct(Math.abs(s.price_change_pct)))} since ${esc(fmtDate(s.price_change_date))}</div>` : ''}</td>
    <td class="r num">${esc(money(s.annual_cost_cents, { whole: true }))}/yr</td><td class="muted small">${esc(fmtDate(s.next_expected_date))}</td>
    <td>${s.user_status === 'cancelled' ? '<span class="chip">cancelled</span>' : s.kind === 'subscription' ? `<button class="btn sm" data-act="cancel-sub" data-merchant="${esc(s.merchant)}">Ask to cancel</button>` : ''}</td></tr>`;
  return `<div class="page-head"><div><h1>Bills & subscriptions</h1><p class="muted">Every statement is audited line by line before anyone pays it.</p></div></div>
  <div class="grid g2">
    <div class="card"><div class="card-head"><h2>Open bills</h2><span class="muted">${bills.open.length}</span></div><div class="stack">${bills.open.map((b) => billCard(b)).join('') || '<div class="empty">No open bills.</div>'}</div></div>
    <div class="card"><div class="card-head"><h2>Bill analyzer</h2><button class="btn sm" data-act="sample-bill">Use a sample bill</button></div>
      <p class="muted small" style="margin-bottom:8px">Paste any bill. Steward checks the arithmetic, the 9% GST, fees and charges that were not on the last bill.</p>
      <textarea id="bill-text" placeholder="Paste bill lines, e.g. 'Plan fee   42.11'"></textarea>
      <div class="row" style="margin-top:8px"><select id="bill-payee"><option value="">Compare with… (optional)</option>${payees.map((p) => `<option>${esc(p.name)}</option>`).join('')}</select><button class="btn primary" data-act="analyze-bill">Analyze</button></div>
      <div id="bill-result" style="margin-top:12px"></div></div>
  </div>
  <div class="card" style="margin-top:16px"><div class="card-head"><h2>Subscriptions</h2><span class="muted">${esc(money(rec.subscriptions_monthly_cents))}/month · ${esc(money(rec.subscriptions_annual_cents, { whole: true }))}/year</span></div>
    <div class="table-wrap"><table><tr><th>Service</th><th>History</th><th class="r">Next charge</th><th class="r">Per year</th><th>Next date</th><th></th></tr>${subs.map(histRow).join('')}</table></div></div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><div class="card-head"><h2>Recurring bills</h2></div><div class="table-wrap"><table>${billsR.map(histRow).join('')}</table></div></div>
    <div class="card"><div class="card-head"><h2>Income streams</h2></div><div class="table-wrap"><table>${income.map(histRow).join('')}</table></div>
      <div class="card-head" style="margin-top:18px"><h2>Billers the agent may pay</h2></div>
      <table>${payees.map((p) => `<tr><td>${esc(p.name)} <span class="muted small">${esc(p.reference)}</span></td><td>${p.verified ? '<span class="chip ok">verified</span>' : '<span class="chip">unverified</span>'}</td>
      <td class="r"><label class="check" style="justify-content:flex-end"><input type="checkbox" data-allow="${p.id}" ${p.agent_allowlisted ? 'checked' : ''}> allowlisted</label></td></tr>`).join('')}</table>
      <p class="small muted" style="margin-top:6px">Adding a biller to the allowlist asks for your password. Autopilot only pays allowlisted, verified billers.</p></div>
  </div>
  <div class="card" style="margin-top:16px"><div class="card-head"><h2>Paid bills</h2></div><div class="table-wrap"><table><tr><th>Biller</th><th>Period</th><th class="r">Amount</th><th>Paid</th><th>By</th><th>Audit</th></tr>
    ${bills.history.map((b) => `<tr><td>${esc(b.payee)}</td><td>${esc(b.period)}</td><td class="r num">${esc(money(b.amount_cents))}</td><td>${esc(fmtDate(b.paid_on))}${b.paid_on > b.due_on ? ' <span class="chip bad">late</span>' : ''}</td><td>${esc(b.paid_by || '')}</td><td><span class="chip ${esc(b.audit.status)}">${esc(label(b.audit.status))}</span></td></tr>`).join('')}</table></div></div>`;
};

// ------------------------------------------------------------------ budgets & goals
PAGES.budgets = async () => {
  const [b, g] = await Promise.all([api('/budgets'), api('/goals')]);
  const ftt = b.suggestions.fifty_thirty_twenty;
  const bar = (name, val, target) => `<div><div class="row between small"><span>${esc(name)}</span><span class="num">${esc(pct(val))} <span class="muted">target ${esc(pct(target))}</span></span></div>
    <div class="bar ${val > target && name !== 'Savings' ? 'warn' : ''}"><span style="width:${Math.min(100, (val || 0) * 100)}%"></span><i class="marker" style="left:${target * 100}%"></i></div></div>`;
  return `<div class="page-head"><div><h1>Budgets & goals</h1><p class="muted">Budgets project your month-end from recurring charges plus your spending pace.</p></div></div>
  <div class="grid g-wide">
    <div class="card"><div class="card-head"><h2>This month</h2><span class="muted">bar = spent · marker = how far through the month we are</span></div>
      <div class="stack">${b.status.map((r) => `<div><div class="row between"><div><b>${esc(r.category)}</b> <span class="chip ${esc(r.status)}">${esc(label(r.status))}</span> ${r.created_by === 'agent' ? '<span class="chip info">set by agent</span>' : ''}</div>
        <div class="row small"><span class="num">${esc(money(r.spent_cents))} of <input class="num" style="width:96px;padding:2px 6px" data-budget="${esc(r.category)}" value="${(r.limit_cents / 100).toFixed(0)}"></span><button class="btn sm link" data-act="del-budget" data-cat="${esc(r.category)}">remove</button></div></div>
        <div class="bar ${r.status === 'over' ? 'bad' : r.status === 'at_risk' ? 'warn' : ''}"><span style="width:${Math.min(100, (r.pct_used || 0) * 100)}%"></span><i class="marker" style="left:${r.month_elapsed_pct * 100}%"></i></div>
        <div class="small muted">Projected month-end ${esc(money(r.projected_cents))}${r.projected_over_cents ? ` · <span class="neg">${esc(money(r.projected_over_cents))} over</span>` : ''}</div></div>`).join('') || '<div class="empty">No budgets yet. Use the suggestions, or let the agent set them at Tier 1+.</div>'}</div></div>
    <div class="stack">
      <div class="card"><div class="card-head"><h2>50 / 30 / 20 check</h2><span class="muted">vs income ${esc(money(b.suggestions.monthly_income_cents))}</span></div>
        <div class="stack">${bar('Needs', ftt.needs_pct, 0.5)}${bar('Wants', ftt.wants_pct, 0.3)}${bar('Savings', ftt.savings_pct, 0.2)}</div></div>
      <div class="card"><div class="card-head"><h2>Suggested budgets</h2><button class="btn sm primary" data-act="apply-suggestions">Apply missing</button></div>
        <table>${b.suggestions.suggestions.map((s) => `<tr><td>${esc(s.category)}<div class="small muted">${esc(s.note || '')}</div></td><td class="r num">${esc(money(s.suggested_cents, { whole: true }))}</td></tr>`).join('')}</table></div>
    </div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><div class="card-head"><h2>Savings goals</h2><span class="muted">safe to save today: ${esc(money(g.safe_to_save_cents))}</span></div>
      <div class="stack">${g.goals.map((x) => `<div><div class="row between"><b>${esc(x.name)}</b><span class="num">${esc(money(x.saved_cents))} / ${esc(money(x.target_cents, { whole: true }))}</span></div>
        <div class="bar"><span style="width:${x.progress_pct * 100}%"></span></div>
        <div class="small muted">${x.required_monthly_cents ? `Needs ${esc(money(x.required_monthly_cents))}/month to hit ${esc(fmtDate(x.target_date, { month: 'short', year: 'numeric' }))} · ${x.on_track ? '<span class="pos">on track</span>' : '<span class="neg">behind</span>'} · ` : ''}${x.projected_completion ? `projected ${esc(fmtDate(x.projected_completion, { month: 'short', year: 'numeric' }))}` : 'add a monthly transfer to project completion'}</div></div>`).join('')}</div>
      <form id="goal-form" class="row" style="margin-top:14px"><input name="name" placeholder="New goal" required maxlength="80" style="flex:1"><input name="target" placeholder="Target S$" required style="width:110px"><input name="date" type="date"><button class="btn" type="submit">Add</button></form></div>
    <div class="card"><div class="card-head"><h2>Can I afford it?</h2></div>
      <form id="afford-form" class="row"><input name="amount" placeholder="Amount, e.g. 1200" required style="flex:1"><input name="date" type="date"><button class="btn primary" type="submit">Check</button></form>
      <div id="afford-result" style="margin-top:12px"></div></div>
  </div>`;
};

// ------------------------------------------------------------------ agent
PAGES.agent = async () => {
  const [pol, actions, history] = await Promise.all([api('/agent/policy'), api('/agent/actions?limit=60'), S.convo ? api(`/agent/chat/${S.convo}`) : Promise.resolve([])]);
  const p = pol.policy;
  const pending = actions.filter((a) => a.status === 'pending_approval');
  const done = actions.filter((a) => a.status !== 'pending_approval');
  S.chat = history.length ? history.map((m) => ({ role: m.role, text: m.content, meta: m.meta })) : S.chat;
  const tierCards = Object.entries(pol.tiers).map(([t, info]) => `<button type="button" class="tier-card ${+t === p.tier ? 'active' : ''}" data-act="pick-tier" data-tier="${t}"><b>Tier ${t} · ${esc(info.name)}</b><span>${esc(info.summary)}</span></button>`).join('');
  const lim = (k, txt) => `<label class="field">${esc(txt)}<input data-limit="${k}" value="${(p[k] / 100).toFixed(0)}"></label>`;
  return `<div class="page-head"><div><h1>Your agent</h1><p class="muted">Chat with Steward, choose how much it may do, and approve what it prepares.</p></div>
    <button class="btn" data-act="run-cycle">Run daily check-up now</button></div>
  <div class="agent-grid">
    <div class="card chat"><div class="card-head"><h2>Ask Steward</h2><span class="muted">${esc(S.meta && S.meta.llm_provider === 'anthropic' ? 'Claude · tool use' : 'offline mode')}</span></div>
      <div class="suggestions">${['Can I afford a $1,200 laptop?', 'What are my subscriptions?', 'Any unusual charges?', 'Pay my StarHub bill', 'Move $200 to savings', 'Send $500 to my friend'].map((q) => `<button class="btn sm" data-act="suggest" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div class="chat-log" id="chat-log">${chatLogHtml()}</div>
      <form class="chat-form" id="chat-form"><input id="chat-input" placeholder="Ask about your money…" maxlength="2000" autocomplete="off"><button class="btn primary" type="submit">Send</button></form></div>
    <div class="stack">
      <div class="card"><div class="card-head"><h2>Waiting for you</h2><span class="muted">${pending.length}</span></div><div class="stack">${pending.map((a) => actionCard(a)).join('') || '<div class="empty">Nothing to approve.</div>'}</div></div>
      <div class="card"><div class="card-head"><h2>Permissions</h2>${p.frozen ? '<span class="chip bad">frozen</span>' : `<span class="chip tier">Tier ${p.tier}</span>`}</div>
        <div class="kill ${p.frozen ? 'on' : ''}"><div><b>Kill switch</b><div class="small muted">${p.frozen ? 'All agent actions are blocked. Unfreezing needs your password.' : 'Instantly stop the agent and cancel everything pending.'}</div></div>
          ${p.frozen ? '<button class="btn" data-act="unfreeze">Unfreeze</button>' : '<button class="btn danger solid" data-act="freeze">Freeze agent</button>'}</div>
        <div class="tier-cards" style="margin-top:12px">${tierCards}</div>
        <div class="grid g2" style="margin-top:12px;gap:10px">${lim('per_action_limit_cents', 'Per action (S$)')}${lim('daily_limit_cents', 'Per day (S$)')}${lim('monthly_limit_cents', 'Per month (S$)')}${lim('min_balance_floor_cents', 'Never go below (S$)')}${lim('step_up_threshold_cents', 'Ask password above (S$)')}</div>
        <div class="row" style="margin-top:12px"><button class="btn primary" data-act="save-policy">Save permissions</button><span class="small muted">Raising the tier or limits asks for your password. Hard cap per action: ${esc(money(pol.hard_ceiling_cents, { whole: true }))}.</span></div>
        <details style="margin-top:10px"><summary>What each tier allows</summary><table class="small"><tr><th>Risk class</th>${[0, 1, 2, 3].map((t) => `<th>T${t}</th>`).join('')}</tr>
        ${[['R1', 'Non-money changes (budgets, categories)'], ['R2', 'Moves between your own accounts'], ['R3', 'Payments, cancellations, disputes']].map(([r, d]) => `<tr><td>${esc(r)} <span class="muted">${esc(d)}</span></td>${[0, 1, 2, 3].map((t) => `<td><span class="chip ${esc(pol.matrix[t][r])}">${esc(label(pol.matrix[t][r]).replace('require approval', 'approve'))}</span></td>`).join('')}</tr>`).join('')}
        <tr><td>R4 <span class="muted">New payees, transfers to people, trading, own limits</span></td>${[0, 1, 2, 3].map(() => '<td><span class="chip deny">never</span></td>').join('')}</tr></table>
        <p class="small muted">At Tier 3 only bill payments to allowlisted billers run unattended; cancellations and disputes still need you. Any failed guardrail (limits, floor, unusual bill) falls back to approval.</p></details></div>
      <div class="card"><div class="card-head"><h2>Recent agent activity</h2></div><div class="stack">${done.slice(0, 15).map((a) => actionCard(a, { buttons: false })).join('') || '<div class="empty">No activity yet.</div>'}</div></div>
    </div>
  </div>`;
};

function chatLogHtml() {
  if (!S.chat.length) return `<div class="msg assistant">Hi ${esc(S.me.display_name)}, I'm Steward. Ask me about your spending, bills or budgets, or ask me to do something. I can only act inside the permission tier you set on the right.</div>`;
  return S.chat.map((m) => `<div class="msg ${m.role}">${esc(m.text)}${m.role === 'assistant' && m.meta ? `<div class="meta">${(m.meta.tools || []).map((t) => `<span class="chip">${esc(t)}</span>`).join('')}${(m.meta.actionViews || []).map((a) => `<span class="chip ${esc(a.status)}">#${a.id} ${esc(label(a.status))}</span>`).join('')}</div>` : ''}</div>`).join('');
}

function wireChat() {
  const form = document.getElementById('chat-form');
  const log = document.getElementById('chat-log');
  if (!form) return;
  log.scrollTop = log.scrollHeight;
  form.addEventListener('submit', (ev) => { ev.preventDefault(); sendChat(document.getElementById('chat-input').value); });
}

async function sendChat(text) {
  text = (text || '').trim();
  if (!text) return;
  const input = document.getElementById('chat-input');
  if (input) input.value = '';
  S.chat.push({ role: 'user', text });
  S.chat.push({ role: 'assistant', text: 'Thinking…' });
  const log = document.getElementById('chat-log');
  if (log) { log.innerHTML = chatLogHtml(); log.scrollTop = log.scrollHeight; }
  try {
    const r = await api('/agent/chat', { method: 'POST', body: { message: text, conversation_id: S.convo } });
    S.convo = r.conversation_id;
    S.chat[S.chat.length - 1] = { role: 'assistant', text: r.reply + (r.note ? `\n\n(${r.note})` : ''), meta: { tools: r.tool_calls.map((t) => t.name), actionViews: r.actions } };
    if (r.actions.length) { await render(); return; }
  } catch (e) { S.chat[S.chat.length - 1] = { role: 'assistant', text: `Sorry, that failed: ${e.message}` }; }
  const l2 = document.getElementById('chat-log');
  if (l2) { l2.innerHTML = chatLogHtml(); l2.scrollTop = l2.scrollHeight; }
}

// ------------------------------------------------------------------ audit
PAGES.audit = async () => {
  const entries = await api('/audit?limit=300');
  return `<div class="page-head"><div><h1>Audit trail</h1><p class="muted">Every login, policy change, agent proposal, decision and execution, hash-chained so tampering is detectable.</p></div>
    <div class="row"><button class="btn primary" data-act="verify">Verify integrity</button><a class="btn" href="/api/v1/audit/export.jsonl">Export JSONL</a></div></div>
  <div id="verify-result"></div>
  <div class="card"><div class="table-wrap"><table><tr><th>#</th><th>Time (UTC)</th><th>Actor</th><th>Event</th><th>Details</th><th>Hash</th></tr>
  ${entries.map((e) => `<tr><td class="num">${e.seq}</td><td class="small muted num">${esc(e.ts.slice(0, 19).replace('T', ' '))}</td><td><span class="chip ${e.actor === 'agent' ? 'info' : ''}">${esc(e.actor)}</span></td><td><b>${esc(e.event)}</b></td>
    <td class="small mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(JSON.stringify(e.payload))}">${esc(JSON.stringify(e.payload))}</td><td class="mono small muted" title="prev ${esc(e.prev_hash)}">${esc(e.hash.slice(0, 12))}…</td></tr>`).join('')}</table></div></div>`;
};

// ------------------------------------------------------------------ sandbox
PAGES.sandbox = async () => {
  const s = await api('/sandbox');
  return `<div class="page-head"><div><h1>Sandbox bank</h1><p class="muted">A simulated bank for a synthetic persona. Fast-forward time to watch the agent work under your current tier.</p></div></div>
  <div class="grid g2"><div class="card"><h2>Simulated date: ${esc(fmtDate(s.sandbox_date, { day: 'numeric', month: 'long', year: 'numeric' }))}</h2>
    <p class="muted" style="margin:8px 0 14px">Each simulated day posts spending, salary and subscriptions, issues bills, marks overdue ones (late charges follow), then runs the agent's daily cycle.</p>
    <div class="row"><button class="btn primary" data-act="advance" data-days="1">+1 day</button><button class="btn" data-act="advance" data-days="7">+7 days</button><button class="btn" data-act="advance" data-days="30">+30 days</button>
    <button class="btn danger" data-act="reset-sandbox">Reset sandbox</button></div><div id="advance-log" style="margin-top:14px"></div></div>
    <div class="card"><h2>Ledger integrity</h2><p class="muted small" style="margin:6px 0 10px">Opening balance + every posted transaction must equal each account's balance.</p>
    <table><tr><th>Account</th><th class="r">Balance</th><th class="r">Opening + postings</th><th></th></tr>${s.ledger.accounts.map((a) => `<tr><td>${esc(a.name)}</td><td class="r num">${esc(money(a.balance_cents))}</td><td class="r num">${esc(money(a.opening_cents + a.posted_sum_cents))}</td><td>${a.consistent ? '<span class="chip ok">consistent</span>' : '<span class="chip bad">mismatch</span>'}</td></tr>`).join('')}</table></div></div>`;
};

// ------------------------------------------------------------------ actions (delegated)
const ACTIONS = {
  'auth-tab': (el) => renderAuth(el.dataset.mode),
  logout: async () => { try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ } S.me = null; S.chat = []; S.convo = null; location.hash = '#/login'; renderAuth('login'); },
  'run-cycle': async () => { const r = await api('/agent/cycle', { method: 'POST' }); toast(`Check-up done: ${r.proposals.length} new proposal(s), ${r.alerts} alert(s).`); rerender(); },
  'insight-action': async (el) => { const a = JSON.parse(el.dataset.payload); const r = await api('/agent/actions', { method: 'POST', body: { action_type: a.action_type, params: a.params, rationale: a.label } }); toast(`${r.summary}: ${label(r.status)}`); rerender(); },
  'agent-pay': async (el) => { const r = await api('/agent/actions', { method: 'POST', body: { action_type: 'pay_bill', params: { bill_id: +el.dataset.id }, rationale: 'User asked the agent to pay this bill.' } }); toast(`${r.summary}: ${label(r.status)}${r.reasons.length ? ' · ' + r.reasons[0] : ''}`); rerender(); },
  'pay-myself': async (el) => { await api(`/bills/${el.dataset.id}/pay`, { method: 'POST' }); toast('Bill paid.'); rerender(); },
  'cancel-sub': async (el) => { const r = await api('/agent/actions', { method: 'POST', body: { action_type: 'cancel_subscription', params: { merchant: el.dataset.merchant }, rationale: 'User asked to cancel.' } }); toast(`${r.summary}: ${label(r.status)}`); rerender(); },
  approve: async (el) => { const r = await api(`/agent/actions/${el.dataset.id}/approve`, { method: 'POST' }); toast(`${r.summary}: ${label(r.status)}`, r.status === 'failed' ? 'error' : ''); rerender(); },
  reject: async (el) => { await api(`/agent/actions/${el.dataset.id}/reject`, { method: 'POST' }); toast('Rejected.'); rerender(); },
  freeze: async () => { const r = await api('/agent/freeze', { method: 'POST' }); toast(`Agent frozen. ${r.cancelled_pending} pending action(s) cancelled.`); rerender(); },
  unfreeze: async () => { await api('/agent/unfreeze', { method: 'POST' }); toast('Agent unfrozen.'); rerender(); },
  'pick-tier': (el) => { document.querySelectorAll('.tier-card').forEach((c) => c.classList.toggle('active', c === el)); },
  'save-policy': async () => {
    const active = document.querySelector('.tier-card.active');
    const body = { tier: +active.dataset.tier };
    document.querySelectorAll('[data-limit]').forEach((i) => { body[i.dataset.limit] = toCents(i.value); });
    await api('/agent/policy', { method: 'PUT', body }); toast('Permissions saved.'); rerender();
  },
  suggest: (el) => sendChat(el.dataset.q),
  'read-alert': async (el) => { await api(`/alerts/${el.dataset.id}/read`, { method: 'POST' }); el.closest('.item').remove(); },
  'tx-page': (el) => { S.txPage = Math.max(0, S.txPage + Number(el.dataset.d)); rerender(); },
  'sample-bill': () => { document.getElementById('bill-text').value = SAMPLE_BILL; document.getElementById('bill-payee').value = 'Singtel'; },
  'analyze-bill': async () => {
    const r = await api('/bills/analyze', { method: 'POST', body: { text: document.getElementById('bill-text').value, payee: document.getElementById('bill-payee').value || null } });
    document.getElementById('bill-result').innerHTML = `<div class="callout ${r.status === 'ok' ? 'ok' : r.status === 'dispute' ? 'bad' : 'warn'}">Verdict: <b>${esc(label(r.status))}</b> · items total ${esc(money(r.computed_total_cents))}${r.stated_total_cents != null ? ` vs stated ${esc(money(r.stated_total_cents))}` : ''} · potential savings ${esc(money(r.potential_savings_cents))}</div>
      <div class="list">${r.findings.map((f) => `<div class="item"><span class="dot ${esc(f.severity)}"></span><div class="grow"><h4>${esc(f.title)}</h4><p>${esc(f.detail)}</p></div></div>`).join('') || '<div class="empty">No issues found.</div>'}</div>`;
  },
  'del-budget': async (el) => { await api(`/budgets/${encodeURIComponent(el.dataset.cat)}`, { method: 'DELETE' }); rerender(); },
  'apply-suggestions': async () => {
    const b = await api('/budgets');
    const have = new Set(b.status.map((r) => r.category));
    const todo = b.suggestions.suggestions.filter((s) => !have.has(s.category) && s.suggested_cents > 0);
    for (const s of todo) await api(`/budgets/${encodeURIComponent(s.category)}`, { method: 'PUT', body: { monthly_limit_cents: s.suggested_cents } });
    toast(`Added ${todo.length} budget(s).`); rerender();
  },
  digest: async () => {
    const box = document.getElementById('digest');
    box.innerHTML = '<div class="card" style="margin-bottom:16px">Writing…</div>';
    const d = await api('/digest');
    box.innerHTML = `<div class="card" style="margin-bottom:16px"><div class="card-head"><h2>${esc(d.headline)}</h2><span class="chip ${d.source === 'llm' ? 'info' : ''}">${esc(d.source)} · grounding ${esc(d.grounding_check)}</span></div>
      <p>${esc(d.summary)}</p><ul>${d.tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>`;
  },
  verify: async () => {
    const v = await api('/audit/verify');
    const a = v.audit_chain, l = v.ledger;
    document.getElementById('verify-result').innerHTML = `<div class="grid g2" style="margin-bottom:16px">
      <div class="callout ${a.ok ? 'ok' : 'bad'}">Audit chain: <b>${a.ok ? 'intact' : 'BROKEN at #' + a.first_bad_seq + ' (' + esc(a.problem) + ')'}</b> · ${a.entries} entries · head ${esc(a.head_hash.slice(0, 16))}…</div>
      <div class="callout ${l.ok ? 'ok' : 'bad'}">Ledger: <b>${l.ok ? 'balanced' : 'MISMATCH'}</b> · ${l.accounts.length} accounts reconcile to their postings</div></div>`;
  },
  advance: async (el) => {
    el.disabled = true;
    const r = await api('/sandbox/advance', { method: 'POST', body: { days: +el.dataset.days } });
    S.me.sandbox_date = r.sandbox_date;
    const lines = r.days.map((d) => {
      const props = (d.agent && d.agent.proposals) || [];
      const bits = [`${d.postings} posting(s)`];
      if (d.bills_issued.length) bits.push(`${d.bills_issued.length} bill(s) issued`);
      if (d.bills_overdue.length) bits.push(`<span class="neg">${d.bills_overdue.length} overdue</span>`);
      if (d.declined.length) bits.push(`<span class="neg">${d.declined.length} declined</span>`);
      props.forEach((p) => bits.push(`<span class="chip ${esc(p.status)}">${esc(p.summary)} · ${esc(label(p.status))}</span>`));
      return `<div class="item"><div class="grow"><b>${esc(fmtDate(d.date, { weekday: 'short', day: 'numeric', month: 'short' }))}</b><p>${bits.join(' · ')}</p></div></div>`;
    });
    await rerender();
    document.getElementById('advance-log').innerHTML = `<div class="list">${lines.join('')}</div>`;
  },
  'reset-sandbox': async () => {
    if (!window.confirm('Reset all sandbox data to a fresh 6-month history? The audit trail is kept.')) return;
    const r = await api('/sandbox/reset', { method: 'POST' }); S.me.sandbox_date = r.sandbox_date; toast('Sandbox reset.'); rerender();
  },
};

document.addEventListener('click', async (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (!fn) return;
  ev.preventDefault();
  try { await fn(el); } catch (e) { fail(e); } finally { if (el.isConnected && el.dataset.act === 'advance') el.disabled = false; }
});

document.addEventListener('change', async (ev) => {
  const el = ev.target;
  try {
    if (el.matches('input[data-allow]')) {
      await api(`/payees/${el.dataset.allow}`, { method: 'PATCH', body: { agent_allowlisted: el.checked } });
      toast(el.checked ? 'Biller allowlisted for the agent.' : 'Biller removed from the allowlist.');
    } else if (el.matches('input[data-budget]')) {
      await api(`/budgets/${encodeURIComponent(el.dataset.budget)}`, { method: 'PUT', body: { monthly_limit_cents: toCents(el.value) } });
      toast(`${el.dataset.budget} budget saved.`); rerender();
    }
  } catch (e) { fail(e); if (el.matches('input[data-allow]')) el.checked = !el.checked; }
});

document.addEventListener('submit', async (ev) => {
  const form = ev.target;
  if (form.id === 'goal-form') {
    ev.preventDefault();
    try { await api('/goals', { method: 'POST', body: { name: form.name.value, target_cents: toCents(form.target.value), target_date: form.date.value || null } }); toast('Goal added.'); rerender(); }
    catch (e) { fail(e); }
  } else if (form.id === 'afford-form') {
    ev.preventDefault();
    try {
      const r = await api('/affordability', { method: 'POST', body: { amount_cents: toCents(form.amount.value), date: form.date.value || null } });
      document.getElementById('afford-result').innerHTML = `<div class="callout ${r.verdict === 'affordable' ? 'ok' : r.verdict === 'risky' ? 'warn' : 'bad'}"><b>${esc(titleCase(r.verdict))}.</b> ${esc(r.reason)}</div>
        <table class="small" style="margin-top:8px"><tr><td>Lowest expected balance after</td><td class="r num">${esc(money(r.min_expected_after_cents))}</td></tr>
        <tr><td>Pessimistic (P10) low after</td><td class="r num">${esc(money(r.min_low_after_cents))}</td></tr><tr><td>Your safety floor</td><td class="r num">${esc(money(r.floor_cents))}</td></tr>
        ${r.pct_of_monthly_discretionary != null ? `<tr><td>Share of a month's discretionary spend</td><td class="r num">${esc(pct(r.pct_of_monthly_discretionary))}</td></tr>` : ''}
        ${r.goal_delay_weeks ? `<tr><td>Delays ${esc(r.goal_delay_weeks.goal)} by</td><td class="r num">${esc(r.goal_delay_weeks.weeks)} weeks</td></tr>` : ''}</table>`;
    } catch (e) { fail(e); }
  }
});

window.addEventListener('hashchange', render);
render();
