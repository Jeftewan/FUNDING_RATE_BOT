let currentTab = 'opportunities';
let currentSubTab = 'cex';
let refreshTimer = null;
const calcCache = {};  // oppId -> { html, capVal, levVal }

// ── Auto-execution state ──────────────────────────────────────
const CEX_EXCHANGES = ['binance', 'bybit', 'okx', 'bitget'];
let _oppMeta = {};            // oppId -> {symbol, mode, exchange, long_exchange, short_exchange, has_spot}
let _cexKeys = new Set();     // lowercased exchange names the user has keys for
let _cexKeysLoaded = false;
let _execContinue = null;     // continuation set by a dry-run result modal

async function loadUserKeys() {
  try {
    const res = await fetch('/api/account');
    if (res.status === 401) return;
    const data = await res.json();
    if (!data.ok) return;
    _cexKeys = new Set((data.exchange_keys || [])
      .filter(k => k.has_key).map(k => (k.exchange || '').toLowerCase()));
    _cexKeysLoaded = true;
  } catch (e) { /* non-fatal */ }
}

// ── Toast notification system ────────────────────────────────
function showToast(msg, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  const icons = { success: '\u2713', error: '\u2717', warning: '\u26A0', info: '\u2139' };
  toast.innerHTML = `<span class="toast-icon">${icons[type] || icons.info}</span><span class="toast-msg">${msg}</span>`;
  toast.onclick = () => dismissToast(toast);
  container.appendChild(toast);
  setTimeout(() => dismissToast(toast), duration);
  return toast;
}

function dismissToast(toast) {
  if (toast.classList.contains('hiding')) return;
  toast.classList.add('hiding');
  toast.addEventListener('animationend', () => toast.remove());
}

function showConfirm(msg) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `
      <div class="confirm-box">
        <div class="confirm-msg">${msg}</div>
        <div class="confirm-actions">
          <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
          <button class="btn btn-primary" data-action="ok">Confirmar</button>
        </div>
      </div>`;
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('[data-action="ok"]').onclick = () => close(true);
    overlay.querySelector('[data-action="cancel"]').onclick = () => close(false);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    const onEsc = (e) => { if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); close(false); } };
    document.addEventListener('keydown', onEsc);
    document.body.appendChild(overlay);
    overlay.querySelector('[data-action="ok"]').focus();
  });
}

// ── Loading skeletons ─────────────────────────────────────────
function showSkeletons(el, count = 3) {
  el.innerHTML = Array.from({length: count}, () => `
    <div class="skeleton-card">
      <div class="skeleton skeleton-line w60"></div>
      <div class="skeleton-grid">
        <div class="skeleton skeleton-line w80"></div>
        <div class="skeleton skeleton-line w40"></div>
        <div class="skeleton skeleton-line w60"></div>
        <div class="skeleton skeleton-line w30"></div>
      </div>
      <div class="skeleton skeleton-line w40" style="margin-top:12px"></div>
    </div>`).join('');
}

// ── Sort & filter state ──────────────────────────────────────
let sortState = { field: 'netapr', dir: 'desc' };
let _lastCexData = null;
let _lastDefiData = null;

function setSort(field) {
  if (sortState.field === field) {
    sortState.dir = sortState.dir === 'desc' ? 'asc' : 'desc';
  } else {
    sortState.field = field;
    sortState.dir = field === 'be' ? 'asc' : 'desc';
  }
  document.querySelectorAll('.sort-btn').forEach(b => {
    const isActive = b.dataset.sort === field;
    b.classList.toggle('active', isActive);
    if (isActive) {
      const arrow = sortState.dir === 'desc' ? ' \u25BC' : ' \u25B2';
      b.textContent = b.textContent.replace(/ [\u25BC\u25B2]/, '') + arrow;
    } else {
      b.textContent = b.textContent.replace(/ [\u25BC\u25B2]/, '');
    }
  });
  applyFilters();
}

function applyFilters() {
  // Invalidate hash so render always happens on user-triggered filter
  _lastCexHash = ''; _lastDefiHash = '';
  if (currentSubTab === 'cex' && _lastCexData) renderOpps(_lastCexData);
  else if (currentSubTab === 'defi' && _lastDefiData) renderDefiOpps(_lastDefiData);
}

// ── Unified opportunity filters (Filtros panel) ──────────────
const ALL_EXCHANGES = ['Binance','Bybit','OKX','Bitget','Hyperliquid','GMX','Aster','Lighter','Extended'];
let _saveFiltersTimer = null;

function getFilterState() {
  const num = (id) => {
    const v = parseFloat(document.getElementById(id)?.value);
    return isNaN(v) ? null : v;
  };
  const exchanges = Array.from(
    document.querySelectorAll('#f-exchanges input:checked')
  ).map(c => c.value);
  return { apr: num('f-apr'),
           days: num('f-days'), vol: num('f-vol'), exchanges };
}

function matchesFilters(o, f) {
  if (f.apr != null && (o.apr || 0) < f.apr) return false;
  if (f.days != null && (o.estimated_hold_days || 0) < f.days) return false;
  if (f.vol != null && f.vol > 0) {
    const v = o.volume_24h || 0;
    // volume 0/unknown (DeFi) passes; only hide known-but-too-small volume.
    if (v > 0 && v < f.vol) return false;
  }
  if (f.exchanges.length) {
    const exs = [o.exchange, o.long_exchange, o.short_exchange].filter(Boolean);
    if (!exs.some(e => f.exchanges.includes(e))) return false;
  }
  return true;
}

function renderExchangeChips(selected) {
  const cont = document.getElementById('f-exchanges');
  if (!cont) return;
  const sel = new Set(selected || []);
  cont.innerHTML = ALL_EXCHANGES.map(ex =>
    `<label class="ex-chip"><input type="checkbox" value="${ex}" ${sel.has(ex) ? 'checked' : ''} onchange="onFilterChange()">${ex}</label>`
  ).join('');
}

function toggleFilters() {
  const p = document.getElementById('filter-panel');
  if (!p) return;
  const show = p.style.display === 'none';
  p.style.display = show ? '' : 'none';
  document.getElementById('btn-filtros')?.classList.toggle('active', show);
}

function resetFilters() {
  ['f-apr','f-days','f-vol'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  renderExchangeChips([]);
  onFilterChange();
}

function onFilterChange() {
  applyFilters();
  if (_saveFiltersTimer) clearTimeout(_saveFiltersTimer);
  _saveFiltersTimer = setTimeout(saveFilters, 800);
}

async function saveFilters() {
  const f = getFilterState();
  const data = {
    min_apr: f.apr != null ? f.apr : 0,
    min_stability_days: f.days != null ? f.days : 0,
    min_volume: f.vol != null ? f.vol : 0,
    allowed_exchanges: f.exchanges.join(','),
  };
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  } catch (e) { console.error('saveFilters error:', e); }
}

async function loadFilters() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el && val != null) el.value = val;
    };
    set('f-apr', cfg.min_apr);
    set('f-days', cfg.min_stability_days);
    set('f-vol', cfg.min_volume);
    const allowed = (cfg.allowed_exchanges || '')
      .split(',').map(s => s.trim()).filter(Boolean);
    renderExchangeChips(allowed);
    applyFilters();
  } catch (e) { console.error('loadFilters error:', e); }
}

function sortAndFilter(opps) {
  const query = (document.getElementById('search-symbol')?.value || '').toLowerCase();
  const f = getFilterState();
  let filtered = opps.filter(o =>
    (!query || o.symbol.toLowerCase().includes(query)) && matchesFilters(o, f)
  );

  const fieldMap = {
    // Ordena por Net APR predicho (número principal mostrado); fallback al score
    // calibrado solo cuando no hay predicción del modelo.
    netapr: o => (o.model_prediction != null ? o.model_prediction : (o.score || 0)),
    apr: o => o.apr || 0,
    fr: o => o.funding_rate || o.rate_differential || 0,
    net3d: o => o.net_3d_revenue_per_1000 || 0,
    volume: o => o.volume_24h || 0,
    be: o => o.break_even_hours || 999,
  };
  const getter = fieldMap[sortState.field] || fieldMap.netapr;
  const mult = sortState.dir === 'desc' ? -1 : 1;
  filtered.sort((a, b) => mult * (getter(a) - getter(b)));
  return filtered;
}

// ── Data hash for anti-flicker ───────────────────────────────
let _lastCexHash = '';
let _lastDefiHash = '';
let _lastPosHash = '';
let _isFirstRender = true;

function oppHash(data) {
  // Hash based on opportunity IDs + scores + scan_count (ignoring mins_to_next which changes every tick)
  const opps = data.opportunities || [];
  const key = opps.map(o => o._id + ':' + (o.model_prediction != null ? o.model_prediction.toFixed(1) : (o.score||0)) + ':' + (o.apr||0).toFixed(1)).join('|');
  return key + '#' + (data.scan_count || 0) + '#' + opps.length;
}

function posHash(data) {
  const pos = data.positions || [];
  const key = pos.map(p => p.symbol + ':' + (p.net_earned||0).toFixed(2) + ':' + (p.payment_count||0)).join('|');
  return key + '#' + pos.length;
}

// Smooth DOM update: fade out briefly, swap, fade in
function smoothUpdate(el, newHTML) {
  if (_isFirstRender) {
    el.innerHTML = newHTML;
    return;
  }
  el.style.opacity = '0.6';
  el.style.transition = 'opacity 0.15s';
  setTimeout(() => {
    el.innerHTML = newHTML;
    el.style.opacity = '1';
    setTimeout(() => { el.style.transition = ''; }, 150);
  }, 80);
}

// ── Earnings chart ───────────────────────────────────────────
let _earningsChart = null;
let _chartMode = 'cumulative';  // 'cumulative' | 'daily'
let _lastDailyEarnings = null;
let _lastPositionsForChart = [];
const CHART_COLORS = ['#22c55e','#3b82f6','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899'];

function setChartMode(mode) {
  _chartMode = (mode === 'daily') ? 'daily' : 'cumulative';
  document.querySelectorAll('.earnings-chart-toggle .chart-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === _chartMode);
  });
  _drawEarningsChart();
}

function renderEarningsChart(positions) {
  _lastPositionsForChart = positions || [];
  _drawEarningsChart();
}

function _drawEarningsChart() {
  const container = document.getElementById('earnings-chart-container');
  const canvas = document.getElementById('earnings-chart');
  if (!container || !canvas || typeof Chart === 'undefined') return;

  if (_earningsChart) { _earningsChart.destroy(); _earningsChart = null; }

  if (_chartMode === 'daily') {
    const series = _lastDailyEarnings?.series || [];
    const hasData = series.some(s => s.earned !== 0);
    if (!hasData) { container.style.display = 'none'; return; }
    container.style.display = '';

    const labels = series.map(s => s.date);
    const data = series.map(s => s.net);
    const colors = data.map(v => v >= 0 ? '#22c55e' : '#ef4444');

    _earningsChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Ganancia neta diaria',
          data,
          backgroundColor: colors,
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => '$' + ctx.parsed.y.toFixed(4) } },
        },
        scales: {
          x: {
            ticks: {
              color: '#555', font: { size: 9 }, maxRotation: 0, autoSkip: true,
              callback: function(v) {
                const lbl = this.getLabelForValue(v);
                const parts = lbl.split('-');
                return parts.length === 3 ? `${parts[2]}/${parts[1]}` : lbl;
              },
            },
            grid: { color: '#1a1d2344' },
          },
          y: {
            ticks: { color: '#555', font: { size: 9 }, callback: v => '$' + v.toFixed(2) },
            grid: { color: '#1a1d2344' },
          },
        },
        animation: { duration: 200 },
      },
    });
    return;
  }

  // Cumulative mode (active positions)
  const withPayments = _lastPositionsForChart.filter(p => p.payments?.length >= 2);
  if (!withPayments.length) { container.style.display = 'none'; return; }
  container.style.display = '';

  const datasets = withPayments.map((p, i) => ({
    label: `${p.symbol} (${p.exchange || p.long_exchange + '/' + p.short_exchange})`,
    data: p.payments
      .filter(pay => !pay.kind)
      .map(pay => ({ x: pay.ts * 1000, y: pay.cumulative })),
    borderColor: CHART_COLORS[i % CHART_COLORS.length],
    backgroundColor: CHART_COLORS[i % CHART_COLORS.length] + '22',
    fill: false,
    borderWidth: 2,
    pointRadius: 2,
    tension: 0.3,
  }));

  _earningsChart = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { color: '#888', font: { size: 10, family: 'JetBrains Mono' }, boxWidth: 12 } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: { color: '#555', font: { size: 9 }, callback: v => new Date(v).toLocaleDateString('es', { day: '2-digit', month: 'short' }) },
          grid: { color: '#1a1d2344' },
        },
        y: {
          ticks: { color: '#555', font: { size: 9 }, callback: v => '$' + v.toFixed(2) },
          grid: { color: '#1a1d2344' },
        },
      },
      animation: { duration: 300 },
    },
  });
}

function renderEarningsKpis(daily) {
  const el = document.getElementById('earnings-kpis');
  if (!el) return;
  if (!daily) { el.innerHTML = ''; return; }

  const today = daily.today?.net || 0;
  const yest = daily.yesterday?.net || 0;
  const last7 = daily.last_7d?.net || 0;
  const last30 = daily.last_30d?.net || 0;
  const total = daily.all_time?.net || 0;
  const apr = daily.realized_apr_7d || 0;

  let deltaPct = 0;
  let deltaTxt = '';
  if (yest !== 0) {
    deltaPct = ((today - yest) / Math.abs(yest)) * 100;
    deltaTxt = `${deltaPct >= 0 ? '+' : ''}${deltaPct.toFixed(0)}% vs ayer`;
  } else if (today !== 0) {
    deltaTxt = 'sin dato ayer';
  }
  const cls = (v) => v > 0 ? 'kpi-positive' : (v < 0 ? 'kpi-negative' : '');
  const fmt = (v) => `${v >= 0 ? '' : '-'}$${Math.abs(v).toFixed(2)}`;

  el.innerHTML = `
    <div class="kpi-card ${cls(today)}">
      <div class="kpi-label">Hoy</div>
      <div class="kpi-value">${fmt(today)}</div>
      <div class="kpi-sub">${deltaTxt || '&nbsp;'}</div>
    </div>
    <div class="kpi-card ${cls(last7)}">
      <div class="kpi-label">7 dias</div>
      <div class="kpi-value">${fmt(last7)}</div>
      <div class="kpi-sub">APR realizado ${apr.toFixed(1)}%</div>
    </div>
    <div class="kpi-card ${cls(last30)}">
      <div class="kpi-label">30 dias</div>
      <div class="kpi-value">${fmt(last30)}</div>
      <div class="kpi-sub">&nbsp;</div>
    </div>
    <div class="kpi-card ${cls(total)}">
      <div class="kpi-label">Total</div>
      <div class="kpi-value">${fmt(total)}</div>
      <div class="kpi-sub">activas + cerradas</div>
    </div>`;
}

async function loadDailyEarnings() {
  try {
    const res = await fetch('/api/earnings/daily?days=30');
    const data = await res.json();
    if (data.ok) {
      _lastDailyEarnings = data;
      renderEarningsKpis(data);
      if (_chartMode === 'daily') _drawEarningsChart();
    }
  } catch (e) {
    console.error('loadDailyEarnings error:', e);
  }
}

// ── Exchange status ──────────────────────────────────────────
let _exchangeStatusTimer = 0;
async function loadExchangeStatus() {
  try {
    const res = await fetch('/api/exchanges/status');
    const data = await res.json();
    const el = document.getElementById('st-exchanges');
    if (!el || !data.exchanges) return;
    el.innerHTML = Object.entries(data.exchanges).map(([name, info]) => {
      const ok = info.status === 'ok' || info.connected;
      return `<span><i class="exchange-dot ${ok ? 'ok' : 'err'}"></i>${name}</span>`;
    }).join('');
  } catch (e) {}
}

// ── Tab switching ─────────────────────────────────────────────
function switchTab(tab) {
  currentTab = tab;
  // Update pages
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  // Update top tabs (desktop)
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  const topTab = document.getElementById('tab-' + tab);
  if (topTab) topTab.classList.add('active');
  // Update bottom nav (mobile)
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const botNav = document.getElementById('bnav-' + tab);
  if (botNav) botNav.classList.add('active');
  // Force fresh render on tab switch
  if (tab === 'opportunities') { _lastCexHash = ''; _lastDefiHash = ''; }
  else if (tab === 'positions') { _lastPosHash = ''; }
  refresh();
}

function switchSubTab(sub) {
  currentSubTab = sub;
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('subtab-' + sub).classList.add('active');
  document.getElementById('opp-list-cex').style.display = sub === 'cex' ? '' : 'none';
  document.getElementById('opp-list-defi').style.display = sub === 'defi' ? '' : 'none';
  loadCurrentOpps();
}

function loadCurrentOpps() {
  if (currentSubTab === 'cex') loadOpps();
  else loadDefiOpps();
}

function refresh() {
  if (currentTab === 'opportunities') loadCurrentOpps();
  else if (currentTab === 'positions') loadPositions();
  else if (currentTab === 'config') loadConfig();
  else if (currentTab === 'account') loadAccount();
}

// ── Auto refresh ──────────────────────────────────────────────
function startRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refresh, 30000);
}

// ── Live counter update (no DOM rebuild) ─────────────────────
function updateLiveCounters(data) {
  // Only update status bar on auto-refresh (no card rebuild)
  updateStatus(data);
  setScanUI(!!data.scanning);
}

// ── Opportunities ─────────────────────────────────────────────
async function loadOpps() {
  const el = document.getElementById('opp-list-cex');
  if (!el.children.length || el.querySelector('.skeleton-card')) showSkeletons(el);
  try {
    const res = await fetch('/api/opportunities');
    const data = await res.json();
    _lastCexData = data;
    // Anti-flicker: skip re-render if opportunities unchanged
    const h = oppHash(data);
    if (h !== _lastCexHash || el.querySelector('.skeleton-card')) {
      _lastCexHash = h;
      renderOpps(data);
    } else {
      // Just update live counters (mins_to_next) without full re-render
      updateLiveCounters(data);
    }
    updateStatus(data);
    setScanUI(!!data.scanning);
  } catch (e) {
    console.error('loadOpps error:', e);
  }
}

function renderOpps(data) {
  const rawOpps = data.opportunities || [];
  const opps = sortAndFilter(rawOpps);
  const el = document.getElementById('opp-list-cex');
  document.getElementById('opp-count').textContent =
    `${opps.length} / ${data.total_unfiltered || 0}`;

  if (!opps.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 16l4-4 3 3 4-5"/></svg>
      <div class="empty-title">Sin oportunidades</div>
      <div class="empty-sub">Esperando escaneo de exchanges...</div>
    </div>`;
    return;
  }

  el.innerHTML = opps.map((o, i) => {
    const mode = o.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Ex';
    const isCross = o.mode === 'cross_exchange';
    const exchange = !isCross ? o.exchange :
      `${o.long_exchange}/${o.short_exchange}`;
    const grade = o.stability_grade || gradeFromNetApr(o.model_prediction != null ? o.model_prediction : o.apr);
    const fr = !isCross ? o.funding_rate : o.rate_differential;
    const frPct = (fr * 100).toFixed(4);
    const days = o.estimated_hold_days || '?';
    // Net APR predicho por el modelo ML — número principal y discriminante.
    // Fallback al APR estimado si el modelo no predijo (model_prediction null).
    const netApr = o.model_prediction != null ? o.model_prediction : o.apr;

    const isExc = o.is_exceptional;
    const excReasons = (o.exceptional_reasons || []).join(' · ');
    const excBadge = isExc ? `<span class="ind-badge ind-exceptional" title="${excReasons}">⭐ EXCEPCIONAL</span>` : '';

    return `
    <div class="opp-card${grade === 'A' ? ' grade-a' : ''}${isExc ? ' exceptional' : ''}">
      <div class="opp-header">
        <div class="opp-header-left">
          <span class="opp-symbol">${o.symbol}</span>
          <span class="opp-mode">${mode}</span>
          <span class="opp-exchange">${exchange}</span>
          ${excBadge}
        </div>
        <div class="opp-header-right">
          <span class="opp-badge ${grade}">${grade}</span>
          <span class="opp-score" title="Net APR predicho (modelo ML)">${netApr.toFixed(1)}%</span>
        </div>
      </div>

      <div class="opp-stats">
        <div class="opp-stat"><span class="label">FR</span><span class="value green">${frPct}%</span></div>
        <div class="opp-stat"><span class="label">APR</span><span class="value green${o.apr > 50 ? ' glow-green' : ''}">${o.apr?.toFixed(1)}%</span></div>
        <div class="opp-stat"><span class="label">3d Acum</span><span class="value">${o.accumulated_3d_pct?.toFixed(3)}%</span></div>
        <div class="opp-stat"><span class="label">$/dia 1K</span><span class="value blue">$${o.daily_income_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Neto 3d</span><span class="value blue">$${o.net_3d_revenue_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Fees</span><span class="value">$${o.fees_total?.toFixed(2) || o.total_fees?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Break-even</span><span class="value">${o.break_even_hours?.toFixed(1)}h</span></div>
        <div class="opp-stat"><span class="label">Hold</span><span class="value">${days}d</span></div>
      </div>

      <div class="opp-meta">
        Vol $${fmtVol(o.volume_24h)}
        ${isCross
          ? ` · L:${o.long_rate ? (o.long_rate*100).toFixed(4)+'%' : '?'} (${o.long_interval_hours||'?'}h) · S:${o.short_rate ? (o.short_rate*100).toFixed(4)+'%' : '?'} (${o.short_interval_hours||'?'}h)`
          : ` · ${o.interval_hours || '?'}h`}
        ${o.mins_to_next > 0 ? ` · ${Math.round(o.mins_to_next)}min` : ''}
      </div>

      <div class="opp-actions">
        <label class="inp-label">Capital<input type="number" id="cap-${i}" placeholder="USD" class="inp-sm" style="width:70px"></label>
        <label class="inp-label">Apal.<input type="number" id="lev-${i}" placeholder="1x" value="1" min="1" max="50" class="inp-sm" style="width:40px"></label>
        <button class="btn btn-calc" onclick="calcEst('${o._id}',${i})">Calc</button>
        <button class="btn btn-enter" onclick="enterPosition('${o._id}',${i})">Entrar</button>
        ${execButton(o, i)}
      </div>
      <div class="opp-est" id="est-${i}"></div>
      ${o.ai_analysis ? `
      <div class="opp-ai" id="ai-${i}">
        <button class="btn-ai-toggle" onclick="this.parentElement.classList.toggle('open')">
          <svg class="ai-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 014 4v1a4 4 0 01-8 0V6a4 4 0 014-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/><circle cx="9" cy="9" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="9" r="1" fill="currentColor" stroke="none"/></svg>
          <span class="ai-label">Analisis IA</span>
          <span class="ai-badge ${o.ai_analysis.signal === 'COMPRAR' ? 'ai-buy' : o.ai_analysis.signal === 'EVITAR' ? 'ai-avoid' : 'ai-hold'}">${o.ai_analysis.signal}</span>
          <span class="ai-conf">${o.ai_analysis.confidence}/10</span>
          <svg class="ai-arrow" width="10" height="10" viewBox="0 0 10 10"><path d="M2 4l3 3 3-3" stroke="currentColor" fill="none" stroke-width="1.5"/></svg>
        </button>
        <div class="ai-body">${o.ai_analysis.analysis}</div>
      </div>` : ''}
    </div>`;
  }).join('');

  // Cache opp metadata for the auto-execution confirm modal.
  opps.forEach(o => {
    _oppMeta[o._id] = {
      symbol: o.symbol, mode: o.mode, exchange: o.exchange,
      long_exchange: o.long_exchange, short_exchange: o.short_exchange,
      has_spot: o.has_spot,
    };
  });

  // Restore cached calculation results and input values
  opps.forEach((o, i) => {
    const cached = calcCache[o._id];
    if (cached) {
      const estEl = document.getElementById('est-' + i);
      if (estEl) estEl.innerHTML = cached.html;
      const capEl = document.getElementById('cap-' + i);
      if (capEl) capEl.value = cached.capVal;
      const levEl = document.getElementById('lev-' + i);
      if (levEl && cached.levVal > 1) levEl.value = cached.levVal;
    }
  });
}

// ── DeFi Opportunities ────────────────────────────────────────
async function loadDefiOpps() {
  const el = document.getElementById('opp-list-defi');
  if (!el.children.length || el.querySelector('.skeleton-card')) showSkeletons(el);
  try {
    const res = await fetch('/api/defi_opportunities');
    const data = await res.json();
    _lastDefiData = data;
    const h = oppHash(data);
    if (h !== _lastDefiHash || el.querySelector('.skeleton-card')) {
      _lastDefiHash = h;
      renderDefiOpps(data);
    }
    updateStatus(data);
    setScanUI(!!data.scanning);
  } catch (e) {
    console.error('loadDefiOpps error:', e);
  }
}

function renderDefiOpps(data) {
  const rawOpps = data.opportunities || [];
  const opps = sortAndFilter(rawOpps);
  const el = document.getElementById('opp-list-defi');
  document.getElementById('opp-count').textContent =
    `${opps.length} DeFi / ${data.total_unfiltered || 0}`;

  if (!opps.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 16l4-4 3 3 4-5"/></svg>
      <div class="empty-title">Sin oportunidades DeFi</div>
      <div class="empty-sub">Esperando escaneo de protocolos...</div>
    </div>`;
    return;
  }

  const base = 1000;
  el.innerHTML = opps.map((o, i) => {
    const idx = base + i;
    const grade = o.stability_grade || gradeFromNetApr(o.model_prediction != null ? o.model_prediction : o.apr);
    const fr = o.rate_differential || 0;
    const frPct = (fr * 100).toFixed(4);

    const defiExs = ['Hyperliquid','GMX','Aster','Lighter','Extended'];
    const le = o.long_exchange || '';
    const se = o.short_exchange || '';
    const isMixed = (defiExs.includes(le)) !== (defiExs.includes(se));
    const modeLabel = isMixed ? 'CEX+DeFi' : 'DeFi';
    const borderColor = isMixed ? 'var(--orange)' : 'var(--purple)';

    const netApr = o.model_prediction != null ? o.model_prediction : o.apr;

    return `
    <div class="opp-card" style="border-left:3px solid ${borderColor}">
      <div class="opp-header">
        <div class="opp-header-left">
          <span class="opp-symbol">${o.symbol}</span>
          <span class="opp-mode">${modeLabel}</span>
          <span class="opp-exchange">${le}/${se}</span>
        </div>
        <div class="opp-header-right">
          <span class="opp-badge ${grade}">${grade}</span>
          <span class="opp-score" title="Net APR predicho (modelo ML)">${netApr.toFixed(1)}%</span>
        </div>
      </div>

      <div class="opp-stats">
        <div class="opp-stat"><span class="label">Diff</span><span class="value green">${frPct}%</span></div>
        <div class="opp-stat"><span class="label">APR</span><span class="value green">${o.apr?.toFixed(1)}%</span></div>
        <div class="opp-stat"><span class="label">3d Acum</span><span class="value">${o.accumulated_3d_pct?.toFixed(3)}%</span></div>
        <div class="opp-stat"><span class="label">$/dia 1K</span><span class="value blue">$${o.daily_income_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Neto 3d</span><span class="value blue">$${o.net_3d_revenue_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Fees</span><span class="value">$${(o.fees_total || o.total_fees)?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Break-even</span><span class="value">${o.break_even_hours?.toFixed(1)}h</span></div>
      </div>

      <div class="opp-meta">
        L:${le} ${o.long_rate ? (o.long_rate*100).toFixed(4)+'%' : '?'} (${o.long_interval_hours||'?'}h) ·
        S:${se} ${o.short_rate ? (o.short_rate*100).toFixed(4)+'%' : '?'} (${o.short_interval_hours||'?'}h)
        ${o.mins_to_next > 0 ? ` · ${Math.round(o.mins_to_next)}min` : ''}
      </div>

      <div class="opp-actions">
        <label class="inp-label">Capital<input type="number" id="cap-${idx}" placeholder="USD" class="inp-sm" style="width:70px"></label>
        <label class="inp-label">Apal.<input type="number" id="lev-${idx}" placeholder="1x" value="1" min="1" max="50" class="inp-sm" style="width:40px"></label>
        <button class="btn btn-calc" onclick="calcEst('${o._id}',${idx})">Calc</button>
        <button class="btn btn-enter" onclick="enterPosition('${o._id}',${idx})">Entrar</button>
      </div>
      <div class="opp-est" id="est-${idx}"></div>
    </div>`;
  }).join('');

  opps.forEach((o, i) => {
    const idx = base + i;
    const cached = calcCache[o._id];
    if (cached) {
      const estEl = document.getElementById('est-' + idx);
      if (estEl) estEl.innerHTML = cached.html;
      const capEl = document.getElementById('cap-' + idx);
      if (capEl) capEl.value = cached.capVal;
      const levEl = document.getElementById('lev-' + idx);
      if (levEl && cached.levVal > 1) levEl.value = cached.levVal;
    }
  });
}

// Fallback de grade cuando el backend no envía stability_grade (datos viejos).
// Mismos umbrales de Net APR que analysis/scoring.grade_from_net_apr (40/20/8).
function gradeFromNetApr(v) {
  if (v == null) return 'D';
  if (v >= 40) return 'A';
  if (v >= 20) return 'B';
  if (v >= 8) return 'C';
  return 'D';
}

function fmtVol(v) {
  if (!v) return '?';
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
  return v.toFixed(0);
}

function renderEntryStrategy(es) {
  if (!es) return '';
  const slipColor = es.slippage_pct > 0.3 ? '#ef4444' : es.slippage_pct > 0.15 ? '#f59e0b' : '#22c55e';
  const srcBadge = es.slippage_source === 'orderbook'
    ? '<span class="es-badge es-live">LIVE</span>'
    : '<span class="es-badge es-est">EST</span>';
  const impactColor = es.book_impact_level === 'high' ? '#ef4444' : es.book_impact_level === 'medium' ? '#f59e0b' : '#22c55e';
  const impactLabel = es.book_impact_level === 'high' ? 'ALTO' : es.book_impact_level === 'medium' ? 'MED' : 'BAJO';
  const winColor = es.window_status === 'green' ? '#22c55e' : es.window_status === 'yellow' ? '#f59e0b' : '#ef4444';
  const winLabel = es.window_status === 'green' ? 'OK' : es.window_status === 'yellow' ? 'PRONTO' : 'URGENTE';
  const winTime = es.mins_to_next > 90 ? `${Math.round(es.mins_to_next / 60)}h` : `${Math.round(es.mins_to_next)}m`;
  let basisTile = '';
  if (es.basis_pct !== null && es.basis_pct !== undefined) {
    const sign = es.basis_pct >= 0 ? '+' : '';
    const basisColor = Math.abs(es.basis_pct) > 0.3 ? '#f59e0b' : '#888';
    basisTile = `<div class="es-tile"><span class="es-label">Basis</span><span class="es-val" style="color:${basisColor}">${sign}${es.basis_pct.toFixed(3)}%</span></div>`;
  }
  return `<div class="entry-strategy">
    <div class="es-grid">
      <div class="es-tile"><span class="es-label">Slippage</span><span class="es-val" style="color:${slipColor}">${es.slippage_pct.toFixed(3)}% ${srcBadge}</span></div>
      <div class="es-tile"><span class="es-label">Impacto libro</span><span class="es-val"><span style="color:${impactColor}">${impactLabel}</span>&nbsp;${es.book_impact_pct.toFixed(3)}%</span></div>
      <div class="es-tile"><span class="es-label">Ventana</span><span class="es-val"><span style="color:${winColor}">${winLabel}</span>&nbsp;${winTime}</span></div>
      ${basisTile}
    </div>
    <div class="es-reco">${es.recommendation}</div>
  </div>`;
}

async function calcEst(oppId, idx) {
  const cap = parseFloat(document.getElementById('cap-' + idx).value);
  const lev = parseInt(document.getElementById('lev-' + idx).value) || 1;
  if (!cap || cap <= 0) { showToast('Ingresa capital', 'warning'); return; }

  const el = document.getElementById('est-' + idx);
  el.textContent = 'Calculando...';

  try {
    const res = await fetch('/api/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opportunity_id: oppId, capital: cap, leverage: lev }),
    });
    const data = await res.json();
    if (data.ok) {
      const e = data.estimate;
      let sizingHtml = '';
      if (e.mode === 'spot_perp' && lev > 1) {
        sizingHtml = `<div style="margin:4px 0;padding:5px 8px;background:#1a1d23;border-radius:6px;font-size:11px;color:#888">
          Spot: <b style="color:#3b82f6">$${e.spot_size?.toFixed(2)}</b> · Margen futures: <b style="color:#f59e0b">$${e.fut_margin?.toFixed(2)}</b> · Exposicion: <b style="color:#fff">$${e.exposure?.toFixed(2)}</b> (${lev}x)
        </div>`;
      } else if (e.mode === 'spot_perp') {
        sizingHtml = `<div style="margin:4px 0;padding:5px 8px;background:#1a1d23;border-radius:6px;font-size:11px;color:#888">
          Spot: <b style="color:#3b82f6">$${e.spot_size?.toFixed(2)}</b> · Short futures: <b style="color:#f59e0b">$${e.fut_margin?.toFixed(2)}</b>
        </div>`;
      } else if (e.mode === 'cross_exchange') {
        sizingHtml = `<div style="margin:4px 0;padding:5px 8px;background:#1a1d23;border-radius:6px;font-size:11px;color:#888">
          Margen/lado: <b style="color:#f59e0b">$${e.margin_per_side?.toFixed(2)}</b> · Exposicion/lado: <b style="color:#fff">$${e.exposure_per_side?.toFixed(2)}</b>${lev > 1 ? ` (${lev}x)` : ''}
        </div>`;
      }

      let html = `
        ${sizingHtml}
        <div style="margin:4px 0">
          <span style="color:#22c55e">$${e.daily_income.toFixed(2)}/dia</span> |
          <span style="color:#22c55e">$${e.income_3day.toFixed(2)}/3d</span> |
          Neto: <span style="color:#fff">$${e.net_3day.toFixed(2)}</span> |
          Fees: $${e.fees_total.toFixed(2)} |
          BE: ${e.break_even_hours.toFixed(1)}h
        </div>`;

      html += renderEntryStrategy(e.entry_strategy);

      if (e.sl_tp) {
        const s = e.sl_tp;
        const p = (v) => v != null ? v.toFixed(4) : '?';
        if (s.mode === 'spot_perp') {
          // Spot-perp: cuando precio sube al SL del short, cierras ambos
          // SL en Perp (pierdes en short) + TP en Spot (vendes con ganancia)
          // Ambos al MISMO precio (arriba de entrada)
          html += `
          <div style="margin-top:6px;padding:6px 8px;background:#1a1d23;border-radius:6px;font-size:12px">
            <div style="color:#888;margin-bottom:4px">Hedge Spot+Perp (entrada: $${p(s.entry_price)})</div>
            <div style="margin-bottom:2px">
              <span style="color:#ef4444">SL Perp (short): $${p(s.perp_sl_price)} (+${s.perp_sl_pct}%)</span>
            </div>
            <div style="margin-bottom:2px">
              <span style="color:#22c55e">TP Spot (vender): $${p(s.spot_tp_price)} (+${s.spot_tp_pct}%)</span>
            </div>
            <div style="color:#666;margin-top:2px">
              Liq perp: $${p(s.perp_liq_price)} (+${s.liq_dist_pct}%) |
              <span style="color:#999">Cierre: ambos al mismo precio</span>
            </div>
          </div>`;
        } else if (s.mode === 'cross_exchange') {
          // Cross-exchange: TP de un lado = SL del otro (cierre conjunto)
          html += `
          <div style="margin-top:6px;padding:6px 8px;background:#1a1d23;border-radius:6px;font-size:12px">
            <div style="color:#888;margin-bottom:4px">Cross-Exchange (liq dist: ${s.liq_dist_pct}%)</div>
            <div style="margin-bottom:3px"><b>LONG</b> (entrada: $${p(s.long_entry)}):
              <br><span style="color:#ef4444">&nbsp;SL: $${p(s.long_sl_price)} (-${s.long_sl_pct}%)</span> &nbsp;
              <span style="color:#22c55e">TP: $${p(s.long_tp_price)} (+${s.long_tp_pct}%)</span>
              <span style="color:#666"> | Liq: $${p(s.long_liq_price)}</span>
            </div>
            <div style="margin-bottom:3px"><b>SHORT</b> (entrada: $${p(s.short_entry)}):
              <br><span style="color:#ef4444">&nbsp;SL: $${p(s.short_sl_price)} (+${s.short_sl_pct}%)</span> &nbsp;
              <span style="color:#22c55e">TP: $${p(s.short_tp_price)} (-${s.short_tp_pct}%)</span>
              <span style="color:#666"> | Liq: $${p(s.short_liq_price)}</span>
            </div>
            <div style="color:#999;margin-top:2px;font-size:11px">
              TP Long = SL Short (precio sube) | TP Short = SL Long (precio baja)
            </div>
          </div>`;
        }
      }

      el.innerHTML = html;
      calcCache[oppId] = { html, capVal: cap, levVal: lev };
    } else {
      el.textContent = data.msg;
      el.style.color = '#ef4444';
    }
  } catch (e) {
    el.textContent = 'Error';
  }
}

async function enterPosition(oppId, idx) {
  const cap = parseFloat(document.getElementById('cap-' + idx).value);
  const lev = parseInt(document.getElementById('lev-' + idx).value) || 1;
  if (!cap || cap <= 0) { showToast('Ingresa capital primero', 'warning'); return; }
  if (!await showConfirm(`Abrir posicion con $${cap} x${lev}?`)) return;

  try {
    const res = await fetch('/api/open_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opportunity_id: oppId, capital: cap, leverage: lev }),
    });
    const data = await res.json();
    if (data.ok) {
      showStepsModal(data);
      loadCurrentOpps();
    } else {
      showToast(data.msg, 'error');
    }
  } catch (e) {
    showToast('Error al abrir posicion', 'error');
  }
}

function showStepsModal(data) {
  document.getElementById('modal-title').textContent =
    `Posicion abierta: ${data.position.symbol}`;
  const steps = (data.steps || []).map(s => `<div class="step">${s}</div>`).join('');
  document.getElementById('modal-body').innerHTML = `
    ${steps}
    ${renderEntryStrategy(data.entry_strategy)}
    <div class="est-summary">
      <div class="est-row"><span>Ganancia diaria estimada</span><span class="est-val">$${data.estimated_daily?.toFixed(2)}</span></div>
      <div class="est-row"><span>Ganancia 3 dias</span><span class="est-val">$${data.estimated_3day?.toFixed(2)}</span></div>
      <div class="est-row"><span>Fees (entrada+salida)</span><span>$${data.fees_total?.toFixed(2)}</span></div>
      <div class="est-row"><span>Break-even</span><span>${data.break_even_hours?.toFixed(1)}h</span></div>
    </div>
    <p style="font-size:11px;color:#f59e0b;margin-top:8px">
      Ejecuta estos pasos manualmente en el exchange. El bot monitoreara los pagos automaticamente.
    </p>`;
  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}

// ── Auto-execution (real orders via API keys) ─────────────────
function posIsCex(p) {
  const legs = p.mode === 'cross_exchange'
    ? [p.long_exchange, p.short_exchange] : [p.exchange];
  return legs.every(e => CEX_EXCHANGES.includes((e || '').toLowerCase()));
}

function execEligibility(meta) {
  // meta: {mode, exchange, long_exchange, short_exchange, has_spot}
  const isCross = meta.mode === 'cross_exchange';
  const legs = isCross ? [meta.long_exchange, meta.short_exchange] : [meta.exchange];
  if (meta.mode === 'defi' || !legs.every(e => CEX_EXCHANGES.includes((e || '').toLowerCase()))) {
    return { ok: false, tip: 'Solo CEX (DeFi / on-chain → flujo manual)' };
  }
  if (!isCross && meta.has_spot === false) {
    return { ok: false, tip: 'Spot no operable por API (Alpha/Onchain/Web3) → manual' };
  }
  if (_cexKeysLoaded) {
    const missing = legs.filter(e => !_cexKeys.has((e || '').toLowerCase()));
    if (missing.length) {
      return { ok: false, tip: `Configura las API keys de ${missing.join(', ')} en la pestaña Cuenta` };
    }
  }
  return { ok: true, tip: 'Coloca las órdenes reales por API' };
}

function execButton(o, i) {
  const el = execEligibility({
    mode: o.mode, exchange: o.exchange,
    long_exchange: o.long_exchange, short_exchange: o.short_exchange,
    has_spot: o.has_spot,
  });
  const dis = el.ok ? '' : 'disabled';
  return `<button class="btn btn-exec" ${dis} title="${el.tip}" onclick="autoExecuteOpen('${o._id}',${i})" data-oid="${o._id}">Ejecutar</button>`;
}

async function autoExecuteOpen(oppId, idx) {
  // idx may be -1 (button built without index): resolve the input by data attr.
  const capEl = idx >= 0 ? document.getElementById('cap-' + idx)
    : document.querySelector(`[data-oid="${oppId}"]`)?.closest('.opp-actions')?.querySelector('input[id^="cap-"]');
  const levEl = idx >= 0 ? document.getElementById('lev-' + idx)
    : document.querySelector(`[data-oid="${oppId}"]`)?.closest('.opp-actions')?.querySelector('input[id^="lev-"]');
  const cap = parseFloat(capEl?.value);
  const lev = parseInt(levEl?.value) || 1;
  if (!cap || cap <= 0) { showToast('Ingresa capital primero', 'warning'); return; }

  const meta = { ..._oppMeta[oppId], capital: cap, leverage: lev };
  const choice = await showExecConfirm('open', meta);
  if (choice === 'cancel') return;
  await runExec('/api/execute_open',
    { opportunity_id: oppId, capital: cap, leverage: lev, dry_run: choice === 'dry' },
    choice === 'dry', 'open', `Apertura ${meta.symbol}`);
}

async function autoExecuteClose(posId, symbol) {
  const p = (_lastPosData?.positions || []).find(x => String(x.id) === String(posId)) || {};
  const meta = {
    symbol, mode: p.mode, exchange: p.exchange,
    long_exchange: p.long_exchange, short_exchange: p.short_exchange, _close: true,
  };
  const choice = await showExecConfirm('close', meta);
  if (choice === 'cancel') return;
  await runExec('/api/execute_close',
    { position_id: posId, dry_run: choice === 'dry' },
    choice === 'dry', 'close', `Cierre ${symbol}`);
}

function showExecConfirm(kind, meta) {
  const isCross = meta.mode === 'cross_exchange';
  const sym = meta.symbol;
  let legsHtml;
  if (kind === 'open') {
    legsHtml = isCross
      ? `<li><strong>Long PERP</strong> ${sym} en <strong>${meta.long_exchange}</strong> · límite</li>
         <li><strong>Short PERP</strong> ${sym} en <strong>${meta.short_exchange}</strong> · límite (90s, aborta si solo 1 llena)</li>`
      : `<li><strong>Long SPOT</strong> ${sym} en <strong>${meta.exchange}</strong> · límite al mid (60s)</li>
         <li><strong>Short PERP</strong> ${sym} en <strong>${meta.exchange}</strong> · market al llenarse el spot</li>`;
  } else {
    legsHtml = isCross
      ? `<li>Cerrar <strong>Long</strong> en ${meta.long_exchange} y <strong>Short</strong> en ${meta.short_exchange} · market</li>`
      : `<li>Vender <strong>SPOT</strong> y cerrar <strong>SHORT perp</strong> en ${meta.exchange} · market</li>`;
  }
  const head = kind === 'open'
    ? `Vas a abrir una posición real x${meta.leverage} con $${meta.capital} de capital.`
    : `Vas a cerrar la posición real de ${sym}.`;

  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `
      <div class="confirm-box exec-box">
        <div class="exec-title">${kind === 'open' ? 'Ejecutar órdenes' : 'Cerrar posición'} — ${sym}</div>
        <div class="exec-sub">${head}</div>
        <ul class="exec-legs">${legsHtml}</ul>
        ${kind === 'open' ? `<div class="exec-note">Asegúrate de tener fondos en la wallet correcta (spot y futures). El bot no transfiere entre wallets.</div>` : ''}
        <div class="exec-warn">⚠ Esto coloca órdenes con dinero real en tu cuenta.</div>
        <div class="confirm-actions exec-actions">
          <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
          <button class="btn btn-calc" data-action="dry" title="Validar sin enviar órdenes">Simular</button>
          <button class="btn btn-danger" data-action="real">${kind === 'open' ? 'Ejecutar órdenes reales' : 'Cerrar de verdad'}</button>
        </div>
      </div>`;
    const close = (val) => { overlay.remove(); document.removeEventListener('keydown', onEsc); resolve(val); };
    overlay.querySelector('[data-action="cancel"]').onclick = () => close('cancel');
    overlay.querySelector('[data-action="dry"]').onclick = () => close('dry');
    overlay.querySelector('[data-action="real"]').onclick = () => close('real');
    overlay.onclick = (e) => { if (e.target === overlay) close('cancel'); };
    const onEsc = (e) => { if (e.key === 'Escape') close('cancel'); };
    document.addEventListener('keydown', onEsc);
    document.body.appendChild(overlay);
    overlay.querySelector('[data-action="cancel"]').focus();
  });
}

async function runExec(url, body, isDry, kind, title) {
  const ov = document.createElement('div');
  ov.className = 'exec-loading';
  ov.innerHTML = `<div class="exec-spinner"></div><div class="exec-loading-txt">${isDry ? 'Simulando…' : 'Ejecutando órdenes…'}</div>`;
  document.body.appendChild(ov);
  try {
    const res = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) {
      // A dry run can be promoted to a real run from the result modal.
      _execContinue = isDry ? () => runExec(url, { ...body, dry_run: false }, false, kind, title) : null;
      showExecResult(title, data, isDry);
      if (!isDry) {
        if (kind === 'open') loadCurrentOpps();
        if (kind === 'close') loadPositions();
      }
    } else {
      showToast(data.msg || 'Ejecución fallida', 'error', 7000);
    }
  } catch (e) {
    showToast('Error de red al ejecutar', 'error');
  } finally {
    ov.remove();
  }
}

function showExecResult(title, data, isDry) {
  const ex = data.exec || {};
  const legs = ex.legs || [];
  const rows = legs.map(l => `
    <tr>
      <td>${l.side === 'buy' ? 'Compra' : 'Venta'} ${l.kind || ''}</td>
      <td>${l.exchange || ''} ${l.symbol || ''}</td>
      <td>${l.amount != null ? (+l.amount).toPrecision(4) : '—'}</td>
      <td>${l.price != null ? '$' + (+l.price).toPrecision(6) : (l.type || '—')}</td>
      <td>${l.fee_usd != null ? '$' + (+l.fee_usd).toFixed(4) : '—'}</td>
    </tr>`).join('');
  const fees = ex.entry_fees_usd != null ? ex.entry_fees_usd
    : (ex.exit_fees_usd != null ? ex.exit_fees_usd : null);
  const ids = (ex.order_ids || []).filter(Boolean).join(', ');

  document.getElementById('modal-title').textContent =
    `${title} ${isDry ? '· SIMULACIÓN' : '· Ejecutado'}`;
  document.getElementById('modal-body').innerHTML = `
    ${isDry ? '<div class="exec-warn" style="margin-bottom:8px">Simulación — no se enviaron órdenes.</div>'
            : '<div class="exec-ok">✓ Órdenes colocadas correctamente.</div>'}
    <table class="exec-table">
      <thead><tr><th>Lado</th><th>Mercado</th><th>Cant.</th><th>Precio</th><th>Fee</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5">Sin detalle de piernas</td></tr>'}</tbody>
    </table>
    <div class="est-summary">
      ${fees != null ? `<div class="est-row"><span>Fees ${data.exec.exit_fees_usd != null ? 'de salida' : 'de entrada'}</span><span class="est-val">$${(+fees).toFixed(4)}</span></div>` : ''}
      ${ids ? `<div class="est-row"><span>Órdenes</span><span style="font-size:10px">${ids}</span></div>` : ''}
    </div>
    ${isDry && _execContinue ? `<button class="btn btn-danger" style="width:100%;margin-top:10px" onclick="(window._runExecContinue&&window._runExecContinue())">Ejecutar órdenes reales</button>` : ''}`;
  document.getElementById('modal').classList.add('open');
}

// Bridge for the dry-run result modal's "execute for real" button.
window._runExecContinue = function () {
  closeModal();
  if (_execContinue) { const fn = _execContinue; _execContinue = null; fn(); }
};

// ── Positions ─────────────────────────────────────────────────
async function loadPositions() {
  const posEl = document.getElementById('pos-list');
  if (!posEl.children.length || posEl.querySelector('.skeleton-card')) showSkeletons(posEl, 2);
  try {
    const [posRes, histRes] = await Promise.all([
      fetch('/api/positions'),
      fetch('/api/history'),
    ]);
    const posData = await posRes.json();
    const histData = await histRes.json();
    const h = posHash(posData);
    if (h !== _lastPosHash || posEl.querySelector('.skeleton-card')) {
      _lastPosHash = h;
      renderPositions(posData);
      renderHistory(histData);
    }
    updateStatus(posData);
    loadDailyEarnings();
  } catch (e) {
    console.error('loadPositions error:', e);
  }
}

let _posAiData = {};
let _posAiLoading = false;
let _lastPosData = null;

async function analyzePositionsAI() {
  if (_posAiLoading) return;
  _posAiLoading = true;
  const btn = document.getElementById('pos-ai-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Analizando posiciones...'; }
  try {
    const res = await fetch('/api/positions/ai', { method: 'POST' });
    const data = await res.json();
    if (data.ok && data.analyses) {
      _posAiData = data.analyses;
      // Update switch analysis in cached position data (on-demand results)
      if (data.switch_results && _lastPosData && _lastPosData.positions) {
        for (const p of _lastPosData.positions) {
          const pid = String(p.id || '');
          if (data.switch_results[pid]) {
            p.switch_analysis = data.switch_results[pid];
          }
        }
      }
      if (_lastPosData) renderPositions(_lastPosData);
      showToast('Analisis IA completado', 'success');
    } else {
      showToast('Sin resultados de IA', 'warning');
    }
  } catch (e) {
    showToast('Error al analizar posiciones', 'error');
  } finally {
    _posAiLoading = false;
    const btn2 = document.getElementById('pos-ai-btn');
    if (btn2) { btn2.disabled = false; btn2.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 014 4v1a4 4 0 01-8 0V6a4 4 0 014-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/></svg> Analizar con IA'; }
  }
}

function renderPositions(data) {
  _lastPosData = data;
  const positions = data.positions || [];
  const summary = data.summary || {};
  const alerts = data.alerts || [];
  renderEarningsChart(positions);

  // Capital bar
  document.getElementById('capital-bar').innerHTML = `
    <div class="cap-item"><div class="cap-val">$${summary.total?.toFixed(0) || 0}</div><div class="cap-label">Total</div></div>
    <div class="cap-item"><div class="cap-val" style="color:var(--blue)">$${summary.used?.toFixed(0) || 0}</div><div class="cap-label">En uso</div></div>
    <div class="cap-item"><div class="cap-val" style="color:var(--green)">$${summary.available?.toFixed(0) || 0}</div><div class="cap-label">Disponible</div></div>
    <div class="cap-item"><div class="cap-val" style="color:var(--green)">$${data.total_earned?.toFixed(2) || 0}</div><div class="cap-label">Ganancia</div></div>
    <div class="cap-item"><div class="cap-val">${summary.count || 0}/${summary.max_positions || 5}</div><div class="cap-label">Pos</div></div>
    <div class="cap-item"><button class="pos-ai-btn" id="pos-ai-btn" onclick="analyzePositionsAI()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 014 4v1a4 4 0 01-8 0V6a4 4 0 014-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/></svg> Analizar con IA</button></div>`;

  // Alerts
  document.getElementById('alerts-bar').innerHTML = alerts.map(a => `
    <div class="alert-item ${a.severity === 'WARNING' ? 'warning' : ''}">
      ${a.symbol} (${a.exchange}): ${a.message}
    </div>`).join('');

  if (alerts.some(a => a.severity === 'CRITICAL')) playBeep();

  const el = document.getElementById('pos-list');
  if (!positions.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M12 10v4m-2-2h4"/></svg>
      <div class="empty-title">Sin posiciones activas</div>
      <div class="empty-sub"><span class="empty-cta" onclick="switchTab('opportunities')">Ir a Oportunidades</span></div>
    </div>`;
    return;
  }

  el.innerHTML = positions.map((p, idx) => {
    const mode = p.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Ex';
    const exchange = p.mode === 'cross_exchange'
      ? `${p.long_exchange}/${p.short_exchange}` : p.exchange;
    const frColor = p.current_fr > 0 ? 'var(--green)' : 'var(--red)';
    const earnColor = p.net_earned >= 0 ? 'var(--green)' : 'var(--red)';
    const posId = p.id || String(idx);

    let alertHtml = '';
    if (p.fr_reversed) {
      alertHtml = '<div class="pos-alert">FR CAMBIO DE SIGNO — CERRAR</div>';
    }

    const payments = (p.payments || []).filter(pay => !pay.kind);
    const lastPayments = payments.slice(-5).reverse();
    let payTable = '';
    if (lastPayments.length) {
      payTable = `
        <div class="pay-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
          ${lastPayments.length} pagos recientes
        </div>
        <div style="display:none">
          <table class="pay-table">
            <tr><th>#</th><th>Hora</th><th>Tasa</th><th>+/-</th><th>Acum</th></tr>
            ${lastPayments.map((pay, i) => `
              <tr>
                <td>${payments.length - i}</td>
                <td>${new Date(pay.ts * 1000).toLocaleTimeString()}</td>
                <td>${(pay.rate * 100).toFixed(4)}%</td>
                <td style="color:${pay.earned >= 0 ? 'var(--green)' : 'var(--red)'}">$${(pay.earned ?? 0).toFixed(4)}</td>
                <td style="color:${pay.cumulative >= 0 ? 'var(--green)' : 'var(--red)'}">$${(pay.cumulative ?? 0).toFixed(2)}</td>
              </tr>`).join('')}
          </table>
        </div>`;
    }

    return `
    <div class="pos-card">
      ${alertHtml}
      <div class="pos-header">
        <div class="pos-header-left">
          <span class="pos-symbol">${p.symbol}</span>
          <span class="opp-mode">${mode}</span>
          <span class="opp-exchange">${exchange}</span>
          ${p.auto_executed ? '<span class="ind-badge badge-auto" title="Posición abierta automáticamente por API">AUTO</span>' : ''}
        </div>
        <div class="pos-header-actions">
          ${posIsCex(p) ? `<button class="btn btn-exec" onclick="autoExecuteClose('${posId}','${p.symbol}')" title="Cerrar colocando órdenes reales">Cerrar (auto)</button>` : ''}
          <button class="btn btn-danger" onclick="closePos('${posId}','${p.symbol}')">Cerrar</button>
        </div>
      </div>

      <div class="pos-grid">
        <div class="pos-field"><span class="label">Capital</span><span class="value">$${p.capital_used.toFixed(0)}</span></div>
        <div class="pos-field"><span class="label">Tiempo</span><span class="value">${p.elapsed_h?.toFixed(1)}h</span></div>
        <div class="pos-field"><span class="label">Pagos</span><span class="value">${p.payment_count || p.intervals || 0}</span></div>
        <div class="pos-field"><span class="label">Prox pago</span><span class="value">${p.mins_next > 0 ? Math.round(p.mins_next) + 'min' : '—'}</span></div>
        <div class="pos-field"><span class="label">FR entrada</span><span class="value">${(p.entry_fr*100).toFixed(4)}%</span></div>
        <div class="pos-field"><span class="label">FR actual</span><span class="value" style="color:${frColor}">${(p.current_fr*100).toFixed(4)}%</span></div>
        <div class="pos-field"><span class="label">APR</span><span class="value" style="color:${frColor}">${p.current_apr?.toFixed(1)}%</span></div>
        <div class="pos-field"><span class="label">Prom</span><span class="value">${p.avg_rate ? (p.avg_rate*100).toFixed(4) + '%' : '—'}</span></div>
        <div class="pos-field"><span class="label">Ganancia</span><span class="value fees-clickable" onclick="editPosEarnings('${posId}')" title="Editar ganancia acumulada (si tu exchange muestra otro total)" style="color:var(--green); cursor:pointer; text-decoration:underline dotted">$${p.est_earned?.toFixed(2)}</span></div>
        <div class="pos-field pos-field-fees">
          <span class="label">
            Fees
            ${p.fees_is_real
              ? '<span class="fee-real" title="Fees reales confirmados">\u2713</span>'
              : '<span class="fee-warn" title="Estos fees son estimados. Pulsa el valor para introducir los reales que pagaste.">\u26A0</span>'}
          </span>
          <span class="value fees-clickable" onclick="editPosFees('${posId}')" title="Editar fees reales" style="color:var(--red); cursor:pointer; text-decoration:underline dotted">
            $${p.est_fees_total?.toFixed(2)}
          </span>
        </div>
        <div class="pos-field"><span class="label">Neto</span><span class="value" style="color:${earnColor};font-weight:700">$${p.net_earned?.toFixed(2)}</span></div>
      </div>

      ${payTable}
      ${p.switch_analysis ? (() => {
        const sa = p.switch_analysis;
        const best = sa.best_switch;
        const health = sa.position_health || {};
        const rec = sa.recommendation;
        const hScore = health.health_score || 0;
        const feePct = health.fee_recovery_pct || 0;
        const trend = health.trend || 'unknown';
        const trendIcon = trend === 'up' ? '\u2191' : trend === 'down' ? '\u2193' : trend === 'stable' ? '\u2192' : '?';
        const trendColor = trend === 'up' ? 'var(--green)' : trend === 'down' ? 'var(--red)' : 'var(--text-secondary)';
        const healthColor = hScore >= 70 ? 'var(--green)' : hScore >= 40 ? 'var(--orange)' : 'var(--red)';
        const feeColor = feePct >= 100 ? 'var(--green)' : feePct >= 50 ? 'var(--orange)' : 'var(--red)';
        const recClass = rec === 'SWITCH' ? 'dp-switch' : rec === 'CONSIDER' ? 'dp-consider' : 'dp-hold';
        const recLabel = rec === 'SWITCH' ? 'CAMBIAR' : rec === 'CONSIDER' ? 'EVALUAR' : 'MANTENER';
        const recIcon = rec === 'SWITCH' ? '\uD83D\uDD04' : rec === 'CONSIDER' ? '\uD83D\uDCA1' : '\u2705';

        let healthReasons = '';
        if (health.reasons_positive && health.reasons_positive.length) {
          healthReasons += health.reasons_positive.map(r => `<span class="dp-reason dp-reason-pos">+ ${r}</span>`).join('');
        }
        if (health.reasons_negative && health.reasons_negative.length) {
          healthReasons += health.reasons_negative.map(r => `<span class="dp-reason dp-reason-neg">- ${r}</span>`).join('');
        }

        let comparisonHtml = '';
        if (best && rec !== 'HOLD') {
          const curProj = sa.current_projected || 0;
          const newProj = best.projected_gain_new || 0;
          const switchCost = best.switch_cost || 0;
          const netNew = newProj - switchCost;
          const improvement = best.improvement_pct || 0;
          comparisonHtml = `
          <div class="dp-comparison">
            <div class="dp-comp-title">Comparacion: Quedarse vs Cambiar</div>
            <div class="dp-comp-table">
              <div class="dp-comp-row dp-comp-header">
                <span></span><span>Actual</span><span>${best.symbol}</span>
              </div>
              <div class="dp-comp-row">
                <span class="dp-comp-label">APR</span>
                <span>${sa.current_apr?.toFixed(1) || '?'}%</span>
                <span style="color:${best.apr > (sa.current_apr || 0) ? 'var(--green)' : 'var(--text-secondary)'}">${best.apr?.toFixed(1)}%</span>
              </div>
              <div class="dp-comp-row">
                <span class="dp-comp-label">Net APR</span>
                <span>${sa.current_net_apr != null ? sa.current_net_apr.toFixed(1) + '%' : '?'}</span>
                <span style="color:${(best.net_apr != null ? best.net_apr : best.score) > (sa.current_net_apr != null ? sa.current_net_apr : (sa.current_score || 0)) ? 'var(--green)' : 'var(--text-secondary)'}">${best.net_apr != null ? best.net_apr.toFixed(1) + '%' : (best.apr != null ? best.apr.toFixed(1) + '%' : '?')}</span>
              </div>
              <div class="dp-comp-row">
                <span class="dp-comp-label">Proy 72h</span>
                <span>$${curProj.toFixed(2)}</span>
                <span style="color:${newProj > curProj ? 'var(--green)' : 'var(--text-secondary)'}">$${newProj.toFixed(2)}</span>
              </div>
              <div class="dp-comp-row">
                <span class="dp-comp-label">Estabilidad</span>
                <span>-</span>
                <span>${best.stability_grade || '?'} (${best.consistency ? best.consistency.toFixed(0) + '%' : '?'})</span>
              </div>
              <div class="dp-comp-row dp-comp-cost">
                <span class="dp-comp-label">Costo switch</span>
                <span colspan="2" style="color:var(--red)">-$${switchCost.toFixed(2)}</span>
                <span>BE: ${best.break_even_h?.toFixed(0)}h</span>
              </div>
              <div class="dp-comp-row dp-comp-result">
                <span class="dp-comp-label">Beneficio neto</span>
                <span></span>
                <span style="color:${best.adjusted_switch_value > 0 ? 'var(--green)' : 'var(--red)'}; font-weight:700">
                  ${best.adjusted_switch_value > 0 ? '+' : ''}$${best.adjusted_switch_value?.toFixed(2)}
                  ${improvement > 0 ? ` (+${improvement.toFixed(0)}%)` : ''}
                </span>
              </div>
            </div>
          </div>`;
        }

        const summary = sa.decision_summary || '';

        return `
        <div class="decision-panel ${recClass}">
          <div class="dp-header" onclick="this.closest('.decision-panel').classList.toggle('dp-open')">
            <div class="dp-header-left">
              <span class="dp-badge ${recClass}">${recIcon} ${recLabel}</span>
              <span class="dp-health-label">Salud</span>
              <span class="dp-health-score" style="color:${healthColor}">${hScore}/100</span>
            </div>
            <div class="dp-expand-btn">
              <svg class="dp-arrow" width="10" height="10" viewBox="0 0 10 10"><path d="M2 4l3 3 3-3" stroke="currentColor" fill="none" stroke-width="1.5"/></svg>
            </div>
          </div>

          <div class="dp-indicators">
            <div class="dp-indicator">
              <span class="dp-ind-label">Fees</span>
              <div class="dp-progress-bar"><div class="dp-progress-fill" style="width:${Math.min(100, feePct)}%;background:${feeColor}"></div></div>
              <span class="dp-ind-val" style="color:${feeColor}">${feePct.toFixed(0)}%</span>
            </div>
            <div class="dp-indicator">
              <span class="dp-ind-label">Tendencia</span>
              <span class="dp-trend" style="color:${trendColor}">${trendIcon} ${trend === 'up' ? 'Subiendo' : trend === 'down' ? 'Bajando' : trend === 'stable' ? 'Estable' : 'Sin datos'}</span>
            </div>
            <div class="dp-indicator">
              <span class="dp-ind-label">FR retenido</span>
              <span class="dp-ind-val" style="color:${(health.rate_retention || 0) >= 80 ? 'var(--green)' : (health.rate_retention || 0) >= 50 ? 'var(--orange)' : 'var(--red)'}">${(health.rate_retention || 0).toFixed(0)}%</span>
            </div>
          </div>

          ${summary ? `<div class="dp-summary">${summary}</div>` : ''}

          <div class="dp-details">
            ${healthReasons ? `<div class="dp-reasons">${healthReasons}</div>` : ''}
            ${comparisonHtml}
          </div>
        </div>`;
      })() : ''}
      ${_posAiData[posId] ? `
      <div class="pos-ai" id="pos-ai-${idx}">
        <button class="btn-ai-toggle" onclick="this.parentElement.classList.toggle('open')">
          <svg class="ai-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 014 4v1a4 4 0 01-8 0V6a4 4 0 014-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/></svg>
          <span class="ai-label">Analisis IA</span>
          <span class="ai-badge ${_posAiData[posId].signal === 'MANTENER' ? 'ai-buy' : _posAiData[posId].signal === 'CERRAR' ? 'ai-avoid' : 'ai-watch'}">${_posAiData[posId].signal}</span>
          <span class="ai-conf">${_posAiData[posId].confidence}/10</span>
          <svg class="ai-arrow" width="10" height="10" viewBox="0 0 10 10"><path d="M2 4l3 3 3-3" stroke="currentColor" fill="none" stroke-width="1.5"/></svg>
        </button>
        <div class="ai-body">
          <div class="ai-analysis">${_posAiData[posId].analysis}</div>
          ${_posAiData[posId].action_plan ? `<div class="ai-action-plan"><span class="ai-action-label">Plan de accion:</span> ${_posAiData[posId].action_plan}</div>` : ''}
        </div>
      </div>` : ''}
    </div>`;
  }).join('');
}

async function closePos(posId, symbol) {
  // Look up the position so we can pre-populate the estimated exit fee
  let estExit = 0;
  let estEntry = 0;
  try {
    const pos = (_lastPosData?.positions || []).find(p => String(p.id) === String(posId));
    if (pos) {
      estExit = pos.exit_fees_effective ?? pos.exit_fees_est ?? 0;
      estEntry = pos.entry_fees_effective ?? pos.entry_fees ?? 0;
    }
  } catch (_) { /* noop */ }

  const confirmed = await showCloseModal(symbol, estEntry, estExit);
  if (!confirmed) return;

  try {
    const res = await fetch('/api/close_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        position_id: posId,
        reason: 'manual',
        exit_fees_real: confirmed.exit_fees_real,
      }),
    });
    const data = await res.json();
    if (data.ok) {
      const r = data.result;
      showToast(`${r.symbol} cerrada — Neto: $${r.net_earned.toFixed(2)} (${r.hours.toFixed(1)}h)`, r.net_earned >= 0 ? 'success' : 'warning', 6000);
      loadPositions();
    } else {
      showToast(data.msg, 'error');
    }
  } catch (e) {
    showToast('Error al cerrar posicion', 'error');
  }
}

// ── Fee editor modal ─────────────────────────────────────────
function showCloseModal(symbol, estEntry, estExit) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `
      <div class="confirm-box" style="max-width:420px">
        <div class="confirm-msg" style="text-align:left">
          <div style="font-size:15px;font-weight:600;margin-bottom:8px">Cerrar posicion ${symbol}</div>
          <div style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
            Opcional: introduce las fees reales de salida que pagaste.
            Si lo dejas vacio, se usa el estimado de $${(estExit || 0).toFixed(2)}.
          </div>
          <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
            Fees reales de salida (USD)
          </label>
          <input type="number" step="0.01" min="0" id="close-exit-fees" placeholder="${(estExit || 0).toFixed(2)}"
                 style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px" />
        </div>
        <div class="confirm-actions">
          <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
          <button class="btn btn-danger" data-action="ok">Cerrar posicion</button>
        </div>
      </div>`;
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('[data-action="ok"]').onclick = () => {
      const raw = overlay.querySelector('#close-exit-fees').value;
      const val = raw === '' ? null : parseFloat(raw);
      close({ exit_fees_real: (isNaN(val) || val < 0) ? null : val });
    };
    overlay.querySelector('[data-action="cancel"]').onclick = () => close(false);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    const onEsc = (e) => { if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); close(false); } };
    document.addEventListener('keydown', onEsc);
    document.body.appendChild(overlay);
    setTimeout(() => overlay.querySelector('#close-exit-fees').focus(), 50);
  });
}

async function editPosFees(posId) {
  const pos = (_lastPosData?.positions || []).find(p => String(p.id) === String(posId));
  if (!pos) { showToast('Posicion no encontrada', 'error'); return; }

  const entryCur = pos.entry_fees_real ?? pos.entry_fees_effective ?? pos.entry_fees ?? 0;
  const exitCur = pos.exit_fees_real ?? pos.exit_fees_effective ?? pos.exit_fees_est ?? 0;
  const entryEst = pos.entry_fees ?? 0;
  const exitEst = pos.exit_fees_est ?? 0;

  const overlay = document.createElement('div');
  overlay.className = 'confirm-overlay';
  overlay.innerHTML = `
    <div class="confirm-box" style="max-width:460px">
      <div class="confirm-msg" style="text-align:left">
        <div style="font-size:15px;font-weight:600;margin-bottom:4px">Fees reales — ${pos.symbol}</div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:14px">
          Introduce las fees que realmente pagaste. Vacio = usar estimado.
        </div>

        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          Fees entrada (USD) <span style="opacity:.6">— estimado $${entryEst.toFixed(2)}</span>
        </label>
        <input type="number" step="0.01" min="0" id="fee-entry"
               value="${pos.entry_fees_real != null ? entryCur.toFixed(2) : ''}"
               placeholder="${entryEst.toFixed(2)}"
               style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;margin-bottom:12px" />

        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          Fees salida (USD) <span style="opacity:.6">— estimado $${exitEst.toFixed(2)}</span>
        </label>
        <input type="number" step="0.01" min="0" id="fee-exit"
               value="${pos.exit_fees_real != null ? exitCur.toFixed(2) : ''}"
               placeholder="${exitEst.toFixed(2)}"
               style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px" />
      </div>
      <div class="confirm-actions">
        <button class="btn btn-secondary" data-action="clear" title="Volver al estimado">Limpiar</button>
        <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
        <button class="btn btn-primary" data-action="ok">Guardar</button>
      </div>
    </div>`;

  const cleanup = () => overlay.remove();
  document.body.appendChild(overlay);
  setTimeout(() => overlay.querySelector('#fee-entry').focus(), 50);

  const send = async (body) => {
    try {
      const res = await fetch(`/api/positions/${encodeURIComponent(posId)}/fees`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.ok) {
        showToast('Fees actualizadas', 'success');
        cleanup();
        loadPositions();
      } else {
        showToast(data.msg || 'Error al actualizar fees', 'error');
      }
    } catch (e) {
      showToast('Error de red al actualizar fees', 'error');
    }
  };

  overlay.querySelector('[data-action="ok"]').onclick = () => {
    const eRaw = overlay.querySelector('#fee-entry').value;
    const xRaw = overlay.querySelector('#fee-exit').value;
    const body = {};
    if (eRaw !== '') {
      const v = parseFloat(eRaw);
      if (isNaN(v) || v < 0) { showToast('Fees de entrada invalidas', 'error'); return; }
      body.entry_fees_real = v;
    } else {
      body.entry_fees_real = null;
    }
    if (xRaw !== '') {
      const v = parseFloat(xRaw);
      if (isNaN(v) || v < 0) { showToast('Fees de salida invalidas', 'error'); return; }
      body.exit_fees_real = v;
    } else {
      body.exit_fees_real = null;
    }
    send(body);
  };
  overlay.querySelector('[data-action="clear"]').onclick = () => {
    send({ entry_fees_real: null, exit_fees_real: null });
  };
  overlay.querySelector('[data-action="cancel"]').onclick = cleanup;
  overlay.onclick = (e) => { if (e.target === overlay) cleanup(); };
  const onEsc = (e) => { if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); cleanup(); } };
  document.addEventListener('keydown', onEsc);
}

async function editPosEarnings(posId) {
  const pos = (_lastPosData?.positions || []).find(p => String(p.id) === String(posId));
  if (!pos) { showToast('Posicion no encontrada', 'error'); return; }

  const curEarned = pos.est_earned ?? pos.earned_real ?? 0;

  const overlay = document.createElement('div');
  overlay.className = 'confirm-overlay';
  overlay.innerHTML = `
    <div class="confirm-box" style="max-width:460px">
      <div class="confirm-msg" style="text-align:left">
        <div style="font-size:15px;font-weight:600;margin-bottom:4px">Ganancia acumulada — ${pos.symbol}</div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:14px">
          Si tu exchange muestra un total distinto, ajustalo aqui. Los proximos pagos
          se sumaran a partir de este valor. Queda registrado en el historial de pagos.
        </div>
        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          Total ganado (USD) <span style="opacity:.6">— actual $${curEarned.toFixed(4)}</span>
        </label>
        <input type="number" step="0.0001" id="earn-total"
               value="${curEarned.toFixed(4)}"
               style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px" />
      </div>
      <div class="confirm-actions">
        <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
        <button class="btn btn-primary" data-action="ok">Guardar</button>
      </div>
    </div>`;

  const cleanup = () => overlay.remove();
  document.body.appendChild(overlay);
  setTimeout(() => overlay.querySelector('#earn-total').focus(), 50);

  overlay.querySelector('[data-action="ok"]').onclick = async () => {
    const raw = overlay.querySelector('#earn-total').value;
    if (raw === '') { showToast('Introduce un valor', 'error'); return; }
    const v = parseFloat(raw);
    if (isNaN(v) || !isFinite(v)) { showToast('Valor invalido', 'error'); return; }
    try {
      const res = await fetch(`/api/positions/${encodeURIComponent(posId)}/earnings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ earned: v }),
      });
      const data = await res.json();
      if (data.ok) {
        showToast(`Ganancia ajustada (${data.delta >= 0 ? '+' : ''}$${data.delta.toFixed(4)})`, 'success');
        cleanup();
        loadPositions();
      } else {
        showToast(data.msg || 'Error al ajustar ganancia', 'error');
      }
    } catch (e) {
      showToast('Error de red', 'error');
    }
  };
  overlay.querySelector('[data-action="cancel"]').onclick = cleanup;
  overlay.onclick = (e) => { if (e.target === overlay) cleanup(); };
  const onEsc = (e) => { if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); cleanup(); } };
  document.addEventListener('keydown', onEsc);
}

async function editHistEarnings(histId) {
  const hist = (_lastHistoryData || []).find(h => String(h.id) === String(histId));
  if (!hist) { showToast('Registro no encontrado', 'error'); return; }

  const curEarned = hist.earned || 0;
  const curFees = hist.fees || 0;

  const overlay = document.createElement('div');
  overlay.className = 'confirm-overlay';
  overlay.innerHTML = `
    <div class="confirm-box" style="max-width:460px">
      <div class="confirm-msg" style="text-align:left">
        <div style="font-size:15px;font-weight:600;margin-bottom:4px">Editar cerrada — ${hist.symbol}</div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:14px">
          Ajusta ganancia y/o fees si descubriste una divergencia con tu exchange.
          El neto se recalcula como ganancia - fees.
        </div>
        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          Ganancia (USD)
        </label>
        <input type="number" step="0.0001" id="hist-earned"
               value="${curEarned.toFixed(4)}"
               style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;margin-bottom:12px" />
        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">
          Fees totales (USD)
        </label>
        <input type="number" step="0.01" min="0" id="hist-fees"
               value="${curFees.toFixed(2)}"
               style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px" />
      </div>
      <div class="confirm-actions">
        <button class="btn btn-secondary" data-action="cancel">Cancelar</button>
        <button class="btn btn-primary" data-action="ok">Guardar</button>
      </div>
    </div>`;

  const cleanup = () => overlay.remove();
  document.body.appendChild(overlay);
  setTimeout(() => overlay.querySelector('#hist-earned').focus(), 50);

  overlay.querySelector('[data-action="ok"]').onclick = async () => {
    const eRaw = overlay.querySelector('#hist-earned').value;
    const fRaw = overlay.querySelector('#hist-fees').value;
    const body = {};
    if (eRaw !== '') {
      const v = parseFloat(eRaw);
      if (isNaN(v) || !isFinite(v)) { showToast('Ganancia invalida', 'error'); return; }
      body.earned = v;
    }
    if (fRaw !== '') {
      const v = parseFloat(fRaw);
      if (isNaN(v) || !isFinite(v) || v < 0) { showToast('Fees invalidos', 'error'); return; }
      body.fees = v;
    }
    if (Object.keys(body).length === 0) { showToast('Nada para guardar', 'warning'); return; }
    try {
      const res = await fetch(`/api/history/${encodeURIComponent(histId)}/earnings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.ok) {
        showToast('Historial actualizado', 'success');
        cleanup();
        loadPositions();
      } else {
        showToast(data.msg || 'Error al actualizar', 'error');
      }
    } catch (e) {
      showToast('Error de red', 'error');
    }
  };
  overlay.querySelector('[data-action="cancel"]').onclick = cleanup;
  overlay.onclick = (e) => { if (e.target === overlay) cleanup(); };
  const onEsc = (e) => { if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); cleanup(); } };
  document.addEventListener('keydown', onEsc);
}

let _lastHistoryData = [];

function exportCSV() {
  if (!_lastHistoryData.length) { showToast('Sin historial para exportar', 'warning'); return; }
  const headers = ['Simbolo','Exchange','Modo','Capital','Exposicion','Apalancamiento','Horas','Pagos','Ganancia','Fees','Neto','Tasa Promedio','Razon','Fecha Cierre'];
  const rows = _lastHistoryData.map(h => [
    h.symbol, h.exchange, h.mode || 'spot_perp',
    h.capital_used?.toFixed(2) || '', h.exposure?.toFixed(2) || '', h.leverage || '1',
    h.hours?.toFixed(1) || '',
    h.payment_count || h.intervals || '', h.earned?.toFixed(4) || '',
    h.fees?.toFixed(4) || '', (h.net_earned || h.earned)?.toFixed(4) || '',
    h.avg_rate ? (h.avg_rate * 100).toFixed(4) + '%' : '',
    h.reason || '', h.closed_at || h.time || '',
  ]);
  const csv = '\uFEFF' + [headers, ...rows].map(r => r.map(c => `"${c}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'historial_funding.csv'; a.click();
  URL.revokeObjectURL(url);
  showToast('CSV exportado', 'success');
}

const HISTORY_PREVIEW_COUNT = 5;

function renderHistory(data) {
  const history = data.history || [];
  _lastHistoryData = history;
  const el = document.getElementById('history-list');
  if (!history.length) {
    el.innerHTML = `<div class="empty-state" style="padding:24px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:32px;height:32px"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
      <div class="empty-title">Sin historial</div>
      <div class="empty-sub">Las posiciones cerradas apareceran aqui</div>
    </div>`;
    return;
  }
  _drawHistoryRows(el, history, HISTORY_PREVIEW_COUNT);
}

function _drawHistoryRows(container, ordered, limit) {
  const visible = ordered.slice(0, limit);
  const rowsHtml = visible.map(h => {
    const net = (h.net_earned ?? h.earned) || 0;
    const netColor = net >= 0 ? 'var(--green, #22c55e)' : 'var(--red, #ef4444)';
    const sign = net >= 0 ? '+' : '';
    const date = h.closed_at ? h.closed_at.split('T')[0] : (h.time?.split('T')[0] || '');
    const hours = (h.hours != null) ? `${h.hours.toFixed(1)}h` : '';
    const pays = h.payment_count || h.intervals || 0;
    const exp = h.exposure ? `$${h.exposure.toFixed(0)}` : '';
    const lev = h.leverage > 1 ? ` ${h.leverage}x` : '';
    const editBtn = h.id
      ? `<button class="hist-edit" onclick="editHistEarnings('${h.id}')" title="Editar ganancia/fees manualmente" aria-label="Editar">✎</button>`
      : '';
    const editedMark = h.notes
      ? ' <span title="Editado manualmente" style="opacity:.6;font-size:11px">✎</span>'
      : '';
    return `
    <div class="hist-item">
      <div class="hist-main">
        <span class="hist-sym">${h.symbol}</span>
        <span class="hist-meta">${h.exchange} · ${h.mode || 'spot_perp'}</span>
      </div>
      <div class="hist-sub">
        <span>${hours}</span>
        <span>${pays} pagos</span>
        ${exp ? `<span>${exp}${lev}</span>` : ''}
        <span>${date}</span>
      </div>
      <div class="hist-net" style="color:${netColor}">${sign}$${net.toFixed(2)}${editedMark}${editBtn}</div>
    </div>`;
  }).join('');

  const hidden = ordered.length - visible.length;
  const isCollapsed = limit <= HISTORY_PREVIEW_COUNT;
  const actionBtn = ordered.length > HISTORY_PREVIEW_COUNT
    ? (isCollapsed
        ? `<button class="btn btn-secondary" id="hist-toggle" style="margin-top:8px;width:100%">Ver más (${hidden})</button>`
        : `<button class="btn btn-secondary" id="hist-toggle" style="margin-top:8px;width:100%">Ver menos</button>`)
    : '';
  container.innerHTML = rowsHtml + actionBtn;

  const btn = document.getElementById('hist-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const nextLimit = isCollapsed ? ordered.length : HISTORY_PREVIEW_COUNT;
      _drawHistoryRows(container, ordered, nextLimit);
    });
  }
}

async function clearHistory(resetAll) {
  const msg = resetAll
    ? 'RESET TOTAL: Borrar historial + cerrar todas las posiciones. Continuar?'
    : 'Borrar todo el historial de posiciones cerradas?';
  if (!await showConfirm(msg)) return;
  try {
    const res = await fetch('/api/clear_history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reset_all: resetAll }),
    });
    const data = await res.json();
    showToast(data.msg || 'Listo', 'success');
    loadPositions();
  } catch (e) {
    showToast('Error al borrar', 'error');
  }
}

// ── Config ────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    document.getElementById('cfg-capital').value = cfg.total_capital;
    document.getElementById('cfg-max-pos').value = cfg.max_positions;
    document.getElementById('cfg-alert-min').value = cfg.alert_minutes_before;
    document.getElementById('cfg-email-on').checked = cfg.email_enabled;
    document.getElementById('cfg-tg-token').value = cfg.tg_bot_token || '';
    document.getElementById('cfg-tg-chatid').value = cfg.tg_chat_id || '';
  } catch (e) {
    console.error('loadConfig error:', e);
  }
}

async function saveConfig() {
  const data = {
    total_capital: parseFloat(document.getElementById('cfg-capital').value),
    max_positions: parseInt(document.getElementById('cfg-max-pos').value),
    alert_minutes_before: parseInt(document.getElementById('cfg-alert-min').value),
    email_enabled: document.getElementById('cfg-email-on').checked,
    tg_bot_token: document.getElementById('cfg-tg-token').value,
    tg_chat_id: document.getElementById('cfg-tg-chatid').value,
  };

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const result = await res.json();
    document.getElementById('cfg-status').textContent = result.msg || 'Guardado';
    setTimeout(() => document.getElementById('cfg-status').textContent = '', 3000);
  } catch (e) {
    document.getElementById('cfg-status').textContent = 'Error';
  }
}

async function testEmail() {
  document.getElementById('email-status').textContent = 'Enviando...';
  document.getElementById('email-status').style.color = '#888';
  try {
    const res = await fetch('/api/test_email', { method: 'POST' });
    const data = await res.json();
    document.getElementById('email-status').textContent = data.msg;
    document.getElementById('email-status').style.color = data.ok ? '#22c55e' : '#ef4444';
  } catch (e) {
    document.getElementById('email-status').textContent = 'Error de conexion';
    document.getElementById('email-status').style.color = '#ef4444';
  }
}

// ── Force scan ────────────────────────────────────────────────
let scanPolling = null;

function setScanUI(scanning) {
  const btn = document.getElementById('btn-scan');
  const bar = document.getElementById('scan-progress');
  if (scanning) {
    btn.disabled = true;
    btn.textContent = 'Escaneando...';
    bar.style.display = 'flex';
  } else {
    btn.disabled = false;
    btn.textContent = 'Escanear';
    bar.style.display = 'none';
    if (scanPolling) { clearInterval(scanPolling); scanPolling = null; }
  }
}

async function forceScan() {
  const btn = document.getElementById('btn-scan');
  if (btn.disabled) return;
  try {
    await fetch('/api/force_scan', { method: 'POST' });
    setScanUI(true);
    // Poll every 3s until scan finishes
    scanPolling = setInterval(async () => {
      try {
        const res = await fetch('/api/opportunities');
        const data = await res.json();
        if (!data.scanning) {
          setScanUI(false);
          renderOpps(data);
          updateStatus(data);
        }
      } catch (e) {}
    }, 3000);
  } catch (e) {}
}

// ── Status bar ────────────────────────────────────────────────
function updateStatus(data) {
  if (data.status) document.getElementById('st-status').innerHTML = '<i class="status-dot"></i>' + data.status;
  if (data.scan_count !== undefined)
    document.getElementById('st-scan').textContent = 'Scan #' + data.scan_count;
  if (data.last_scan) {
    let display = data.last_scan;
    const v = Number(data.last_scan);
    if (!isNaN(v) && v > 1e9) {
      const d = new Date(v * 1000);
      display = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    }
    document.getElementById('st-time').textContent = display;
  }
}

// ── Audio alert ───────────────────────────────────────────────
function playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 800;
    gain.gain.value = 0.3;
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch (e) {}
}

// ── Account & Exchange Keys (SaaS) ───────────────────────────
const EXCHANGES = ['Binance', 'Bybit', 'OKX', 'Bitget'];

async function loadAccount() {
  const el = document.getElementById('exchange-keys-list');
  if (!el) return;
  try {
    const res = await fetch('/api/account');
    if (res.status === 401) return;
    const data = await res.json();
    if (!data.ok) return;

    const keyMap = {};
    (data.exchange_keys || []).forEach(k => { keyMap[k.exchange] = k.has_key; });

    el.innerHTML = EXCHANGES.map(ex => {
      const hasKey = keyMap[ex] || false;
      const statusIcon = hasKey ? '<span style="color:#22c55e">&#10003; Configurada</span>' : '<span style="color:#555">No configurada</span>';
      return `
        <div class="cfg-section" style="margin:6px 0;padding:12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <strong style="font-size:12px;color:#fff">${ex}</strong>
            ${statusIcon}
          </div>
          <div class="cfg-grid" style="grid-template-columns:1fr 1fr">
            <label>API Key<input type="password" id="ek-key-${ex}" class="inp" placeholder="${hasKey ? '••••••••' : 'API Key'}"></label>
            <label>API Secret<input type="password" id="ek-secret-${ex}" class="inp" placeholder="${hasKey ? '••••••••' : 'API Secret'}"></label>
            ${ex === 'OKX' || ex === 'Bitget' ? `<label>Passphrase<input type="password" id="ek-pass-${ex}" class="inp" placeholder="${hasKey ? '••••••••' : 'Passphrase'}"></label>` : ''}
          </div>
          <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-primary" onclick="saveExchangeKey('${ex}')" style="font-size:11px;padding:4px 12px">Guardar</button>
            ${hasKey ? `<button class="btn btn-calc" onclick="testExchangeKey('${ex}')" style="font-size:11px;padding:4px 12px">Probar conexión</button>` : ''}
            ${hasKey ? `<button class="btn btn-danger" onclick="deleteExchangeKey('${ex}')" style="font-size:11px;padding:4px 12px">Eliminar</button>` : ''}
            <span id="ek-status-${ex}" style="font-size:11px;align-self:center"></span>
          </div>
        </div>`;
    }).join('');
    // Keep the auto-execution key set in sync with what's configured.
    _cexKeys = new Set((data.exchange_keys || [])
      .filter(k => k.has_key).map(k => (k.exchange || '').toLowerCase()));
    _cexKeysLoaded = true;
  } catch (e) {
    console.error('loadAccount error:', e);
  }
}

async function saveExchangeKey(exchange) {
  const key = document.getElementById('ek-key-' + exchange)?.value || '';
  const secret = document.getElementById('ek-secret-' + exchange)?.value || '';
  const pass = document.getElementById('ek-pass-' + exchange)?.value || '';
  if (!key && !secret) { showToast('Ingresa al menos API Key y Secret', 'warning'); return; }
  try {
    const res = await fetch('/api/account/exchange_keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exchange, api_key: key, api_secret: secret, passphrase: pass }),
    });
    const data = await res.json();
    showToast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) loadAccount();
  } catch (e) {
    showToast('Error al guardar keys', 'error');
  }
}

async function testExchangeKey(exchange) {
  const statusEl = document.getElementById('ek-status-' + exchange);
  if (statusEl) { statusEl.textContent = 'Probando…'; statusEl.style.color = '#888'; }
  try {
    const res = await fetch('/api/account/exchange_keys/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exchange }),
    });
    const data = await res.json();
    if (statusEl) {
      statusEl.style.color = data.ok ? '#22c55e' : '#ef4444';
      const bal = data.usdt_balance != null ? ` · USDT: ${(+data.usdt_balance).toFixed(2)}` : '';
      statusEl.textContent = (data.ok ? '✓ ' : '✗ ') + (data.msg || '') + (data.ok ? bal : '');
    }
    showToast(data.msg || (data.ok ? 'Conexión OK' : 'Conexión fallida'),
      data.ok ? 'success' : 'error', 6000);
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#ef4444'; statusEl.textContent = '✗ Error de red'; }
    showToast('Error al probar conexión', 'error');
  }
}

async function deleteExchangeKey(exchange) {
  if (!await showConfirm(`Eliminar API keys de ${exchange}?`)) return;
  try {
    const res = await fetch('/api/account/exchange_keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exchange, api_key: '', api_secret: '' }),
    });
    const data = await res.json();
    showToast(data.msg, data.ok ? 'success' : 'error');
    if (data.ok) loadAccount();
  } catch (e) {
    showToast('Error al eliminar keys', 'error');
  }
}

async function deleteAccount() {
  if (!await showConfirm('ELIMINAR CUENTA: Se borraran todos tus datos permanentemente. Continuar?')) return;
  try {
    const res = await fetch('/api/account', { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) window.location.href = '/';
    else showToast(data.msg, 'error');
  } catch (e) {
    showToast('Error al eliminar cuenta', 'error');
  }
}

// ── Auth ─────────────────────────────────────────────────────
async function doLogout() {
  try {
    await fetch('/auth/logout', { method: 'POST' });
  } catch (e) {}
  window.location.href = '/';
}

// ── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  refresh();
  // Mark first render done after initial load completes
  setTimeout(() => { _isFirstRender = false; }, 2000);
  startRefresh();
  loadFilters();
  loadUserKeys();
  loadExchangeStatus();
  setInterval(loadExchangeStatus, 300000);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
});
