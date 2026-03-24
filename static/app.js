let currentTab = 'opportunities';
let currentSubTab = 'cex';
let refreshTimer = null;
const calcCache = {};  // oppId -> { html, capVal, levVal }

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
let sortState = { field: 'score', dir: 'desc' };
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
  if (currentSubTab === 'cex' && _lastCexData) renderOpps(_lastCexData);
  else if (currentSubTab === 'defi' && _lastDefiData) renderDefiOpps(_lastDefiData);
}

function sortAndFilter(opps) {
  const query = (document.getElementById('search-symbol')?.value || '').toLowerCase();
  let filtered = query ? opps.filter(o => o.symbol.toLowerCase().includes(query)) : [...opps];

  const fieldMap = {
    score: o => o.score || 0,
    apr: o => o.apr || 0,
    fr: o => o.funding_rate || o.rate_differential || 0,
    net3d: o => o.net_3d_revenue_per_1000 || 0,
    volume: o => o.volume_24h || 0,
    be: o => o.break_even_hours || 999,
  };
  const getter = fieldMap[sortState.field] || fieldMap.score;
  const mult = sortState.dir === 'desc' ? -1 : 1;
  filtered.sort((a, b) => mult * (getter(a) - getter(b)));
  return filtered;
}

// ── Mini-charts ──────────────────────────────────────────────
const _chartCache = {};     // symbol_exchange -> { rates, timestamps }
const _chartInstances = {}; // canvasId -> Chart instance
let _chartQueue = [];
let _chartActive = 0;
const MAX_CONCURRENT_CHARTS = 3;

function initMiniChartObserver() {
  if (!window.IntersectionObserver || typeof Chart === 'undefined') return;
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const el = entry.target;
        const symbol = el.dataset.chartSymbol;
        const exchange = el.dataset.chartExchange;
        if (symbol && exchange) {
          observer.unobserve(el);
          enqueueChart(el, symbol, exchange);
        }
      }
    });
  }, { rootMargin: '100px' });

  document.querySelectorAll('.mini-chart[data-chart-symbol]').forEach(el => observer.observe(el));
}

function enqueueChart(el, symbol, exchange) {
  _chartQueue.push({ el, symbol, exchange });
  processChartQueue();
}

async function processChartQueue() {
  while (_chartQueue.length && _chartActive < MAX_CONCURRENT_CHARTS) {
    _chartActive++;
    const { el, symbol, exchange } = _chartQueue.shift();
    try { await loadMiniChart(el, symbol, exchange); } catch (e) {}
    _chartActive--;
  }
}

async function loadMiniChart(el, symbol, exchange) {
  const key = `${symbol}_${exchange}`;
  let data = _chartCache[key];
  if (!data) {
    try {
      const res = await fetch(`/api/funding_history/${encodeURIComponent(symbol)}/${encodeURIComponent(exchange)}`);
      data = await res.json();
      if (data.rates?.length) _chartCache[key] = data;
    } catch (e) { return; }
  }
  if (!data?.rates?.length) { el.innerHTML = ''; return; }
  renderMiniChart(el, data.rates, data.timestamps);
}

function renderMiniChart(container, rates, timestamps) {
  const canvas = document.createElement('canvas');
  container.innerHTML = '';
  container.appendChild(canvas);

  const avgRate = rates.reduce((a, b) => a + b, 0) / rates.length;
  const color = avgRate >= 0 ? '#22c55e' : '#ef4444';

  const id = container.id || 'mc-' + Math.random().toString(36).slice(2);
  if (_chartInstances[id]) { _chartInstances[id].destroy(); delete _chartInstances[id]; }

  _chartInstances[id] = new Chart(canvas, {
    type: 'line',
    data: {
      labels: timestamps.map(t => ''),
      datasets: [{
        data: rates.map(r => r * 100),
        borderColor: color,
        backgroundColor: color + '11',
        fill: true,
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
      }],
    },
    options: {
      responsive: false,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { display: false },
        y: { display: false },
      },
      animation: { duration: 300 },
    },
  });
}

// ── Earnings chart ───────────────────────────────────────────
let _earningsChart = null;
const CHART_COLORS = ['#22c55e','#3b82f6','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899'];

function renderEarningsChart(positions) {
  const container = document.getElementById('earnings-chart-container');
  const canvas = document.getElementById('earnings-chart');
  if (!container || !canvas || typeof Chart === 'undefined') return;

  const withPayments = (positions || []).filter(p => p.payments?.length >= 2);
  if (!withPayments.length) { container.style.display = 'none'; return; }
  container.style.display = '';

  if (_earningsChart) { _earningsChart.destroy(); _earningsChart = null; }

  const datasets = withPayments.map((p, i) => ({
    label: `${p.symbol} (${p.exchange || p.long_exchange + '/' + p.short_exchange})`,
    data: p.payments.map(pay => ({ x: pay.ts * 1000, y: pay.cumulative })),
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
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
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

// ── Opportunities ─────────────────────────────────────────────
async function loadOpps() {
  const el = document.getElementById('opp-list-cex');
  if (!el.children.length || el.querySelector('.skeleton-card')) showSkeletons(el);
  try {
    const res = await fetch('/api/opportunities');
    const data = await res.json();
    _lastCexData = data;
    renderOpps(data);
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
    `${opps.length} oportunidades (${data.total_unfiltered || 0} total)`;

  if (!opps.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 16l4-4 3 3 4-5"/></svg>
      <div class="empty-title">Sin oportunidades</div>
      <div class="empty-sub">Esperando escaneo de exchanges...</div>
    </div>`;
    return;
  }

  el.innerHTML = opps.map((o, i) => {
    const mode = o.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Exchange';
    const isCross = o.mode === 'cross_exchange';
    const exchange = !isCross ? o.exchange :
      `${o.long_exchange} (${o.long_interval_hours||'?'}h) / ${o.short_exchange} (${o.short_interval_hours||'?'}h)`;
    const grade = o.stability_grade || gradeFromScore(o.score);
    const fr = !isCross ? o.funding_rate : o.rate_differential;
    const frPct = (fr * 100).toFixed(4);
    const days = o.estimated_hold_days || '?';

    return `
    <div class="opp-card${grade === 'A' ? ' grade-a' : ''}">
      <div class="opp-header">
        <div>
          <span class="opp-symbol">${o.symbol}/USDT</span>
          <span class="opp-mode">${mode}</span>
          <span style="font-size:11px;color:#888;margin-left:6px">${exchange}</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span class="opp-badge ${grade}">${grade}</span>
          <span style="font-size:12px;color:#fff;font-weight:700">${o.score}/100</span>
        </div>
      </div>

      <div class="opp-stats">
        <div class="opp-stat"><span class="label" data-tooltip="Tasa de financiacion actual del contrato perpetuo">FR</span><span class="value green">${frPct}%</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Tasa anualizada basada en la tasa actual">APR</span><span class="value green${o.apr > 50 ? ' glow-green' : ''}">${o.apr?.toFixed(1)}%</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Tasa acumulada en los ultimos 3 dias">3d Acum</span><span class="value">${o.accumulated_3d_pct?.toFixed(3)}%</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Ingreso diario estimado por cada $1,000">$/dia (1K)</span><span class="value blue">$${o.daily_income_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Ganancia neta en 3 dias por $1,000 (menos fees)">Neto 3d (1K)</span><span class="value blue">$${o.net_3d_revenue_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Costos de entrada + salida estimados">Fees</span><span class="value">$${o.fees_total?.toFixed(2) || o.total_fees?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Horas necesarias para cubrir los costos de entrada y salida">Break-even</span><span class="value">${o.break_even_hours?.toFixed(1)}h</span></div>
        <div class="opp-stat"><span class="label" data-tooltip="Dias estimados de hold recomendado">Est. dias</span><span class="value">${days}d</span></div>
      </div>

      <div class="opp-meta">
        Vol: $${fmtVol(o.volume_24h)} |
        ${isCross
          ? `Long ${o.long_rate ? (o.long_rate*100).toFixed(4)+'%' : '?'} (${o.long_interval_hours||'?'}h) · Short ${o.short_rate ? (o.short_rate*100).toFixed(4)+'%' : '?'} (${o.short_interval_hours||'?'}h)`
          : `Intervalo: ${o.interval_hours || '?'}h`} |
        ${o.mins_to_next > 0 ? `Prox pago: ${Math.round(o.mins_to_next)}min` : ''}
      </div>

      <div class="mini-chart" id="mc-cex-${i}" data-chart-symbol="${o.symbol}" data-chart-exchange="${!isCross ? o.exchange : o.short_exchange}"></div>

      <div class="opp-actions">
        <input type="number" id="cap-${i}" placeholder="Capital $" class="inp-sm" style="width:90px">
        <input type="number" id="lev-${i}" placeholder="Lev" value="1" min="1" max="50" class="inp-sm" style="width:55px" title="Apalancamiento">
        <button class="btn btn-calc" onclick="calcEst('${o._id}',${i})">Calcular</button>
        <button class="btn btn-enter" onclick="enterPosition('${o._id}',${i})">Entrar</button>
      </div>
      <div class="opp-est" id="est-${i}"></div>
    </div>`;
  }).join('');

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
  setTimeout(initMiniChartObserver, 50);
}

// ── DeFi Opportunities ────────────────────────────────────────
async function loadDefiOpps() {
  const el = document.getElementById('opp-list-defi');
  if (!el.children.length || el.querySelector('.skeleton-card')) showSkeletons(el);
  try {
    const res = await fetch('/api/defi_opportunities');
    const data = await res.json();
    _lastDefiData = data;
    renderDefiOpps(data);
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
    `${opps.length} oportunidades DeFi (${data.total_unfiltered || 0} total)`;

  if (!opps.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 16l4-4 3 3 4-5"/></svg>
      <div class="empty-title">Sin oportunidades DeFi</div>
      <div class="empty-sub">Esperando escaneo de protocolos DeFi...</div>
    </div>`;
    return;
  }

  // DeFi opps are cross-exchange, use index offset to avoid ID collision with CEX
  const base = 1000;
  el.innerHTML = opps.map((o, i) => {
    const idx = base + i;
    const isCross = true; // DeFi opps are always cross-exchange
    const exchange = `${o.long_exchange} / ${o.short_exchange}`;
    const grade = o.stability_grade || gradeFromScore(o.score);
    const fr = o.rate_differential || 0;
    const frPct = (fr * 100).toFixed(4);

    // Detect mixed CEX+DeFi
    const defiExs = ['Hyperliquid','GMX','Aster','Lighter','Extended'];
    const le = o.long_exchange || '';
    const se = o.short_exchange || '';
    const isMixed = (defiExs.includes(le)) !== (defiExs.includes(se));
    const modeLabel = isMixed ? 'CEX+DeFi' : 'DeFi-DeFi';

    return `
    <div class="opp-card" style="border-left:3px solid ${isMixed ? '#f59e0b' : '#8b5cf6'}">
      <div class="opp-header">
        <div>
          <span class="opp-symbol">${o.symbol}/USDT</span>
          <span class="opp-mode" style="background:${isMixed ? '#422006' : '#1e1b4b'};color:${isMixed ? '#f59e0b' : '#a78bfa'}">${modeLabel}</span>
          <span style="font-size:11px;color:#888;margin-left:6px">${exchange}</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span class="opp-badge ${grade}">${grade}</span>
          <span style="font-size:12px;color:#fff;font-weight:700">${o.score}/100</span>
        </div>
      </div>

      <div class="opp-stats">
        <div class="opp-stat"><span class="label">Diff</span><span class="value green">${frPct}%</span></div>
        <div class="opp-stat"><span class="label">APR</span><span class="value green">${o.apr?.toFixed(1)}%</span></div>
        <div class="opp-stat"><span class="label">3d Acum</span><span class="value">${o.accumulated_3d_pct?.toFixed(3)}%</span></div>
        <div class="opp-stat"><span class="label">$/dia (1K)</span><span class="value blue">$${o.daily_income_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Neto 3d (1K)</span><span class="value blue">$${o.net_3d_revenue_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Fees</span><span class="value">$${(o.fees_total || o.total_fees)?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Break-even</span><span class="value">${o.break_even_hours?.toFixed(1)}h</span></div>
      </div>

      <div class="opp-meta">
        Long: ${le} ${o.long_rate ? (o.long_rate*100).toFixed(4)+'%' : '?'} (${o.long_interval_hours||'?'}h) ·
        Short: ${se} ${o.short_rate ? (o.short_rate*100).toFixed(4)+'%' : '?'} (${o.short_interval_hours||'?'}h)
        ${o.mins_to_next > 0 ? ` | Prox pago: ${Math.round(o.mins_to_next)}min` : ''}
      </div>

      <div class="mini-chart" id="mc-defi-${idx}" data-chart-symbol="${o.symbol}" data-chart-exchange="${se}"></div>

      <div class="opp-actions">
        <input type="number" id="cap-${idx}" placeholder="Capital $" class="inp-sm" style="width:90px">
        <input type="number" id="lev-${idx}" placeholder="Lev" value="1" min="1" max="50" class="inp-sm" style="width:55px" title="Apalancamiento">
        <button class="btn btn-calc" onclick="calcEst('${o._id}',${idx})">Calcular</button>
        <button class="btn btn-enter" onclick="enterPosition('${o._id}',${idx})">Entrar</button>
      </div>
      <div class="opp-est" id="est-${idx}"></div>
    </div>`;
  }).join('');

  // Restore cached calculation results and input values
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
  setTimeout(initMiniChartObserver, 50);
}

function gradeFromScore(s) {
  if (s >= 85) return 'A';
  if (s >= 70) return 'B';
  if (s >= 55) return 'C';
  return 'D';
}

function fmtVol(v) {
  if (!v) return '?';
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
  return v.toFixed(0);
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
      let html = `
        <div style="margin:4px 0">
          <span style="color:#22c55e">$${e.daily_income.toFixed(2)}/dia</span> |
          <span style="color:#22c55e">$${e.income_3day.toFixed(2)}/3d</span> |
          Neto: <span style="color:#fff">$${e.net_3day.toFixed(2)}</span> |
          Fees: $${e.fees_total.toFixed(2)} |
          BE: ${e.break_even_hours.toFixed(1)}h
          ${lev > 1 ? ` | Pos: $${e.position_size?.toFixed(0)}` : ''}
        </div>`;

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
    renderPositions(posData);
    renderHistory(histData);
    updateStatus(posData);
  } catch (e) {
    console.error('loadPositions error:', e);
  }
}

function renderPositions(data) {
  const positions = data.positions || [];
  const summary = data.summary || {};
  const alerts = data.alerts || [];
  renderEarningsChart(positions);

  // Capital bar
  document.getElementById('capital-bar').innerHTML = `
    <div class="cap-item"><div class="cap-val">$${summary.total?.toFixed(0) || 0}</div><div class="cap-label">Capital total</div></div>
    <div class="cap-item"><div class="cap-val" style="color:#3b82f6">$${summary.used?.toFixed(0) || 0}</div><div class="cap-label">En uso</div></div>
    <div class="cap-item"><div class="cap-val" style="color:#22c55e">$${summary.available?.toFixed(0) || 0}</div><div class="cap-label">Disponible</div></div>
    <div class="cap-item"><div class="cap-val" style="color:#22c55e">$${data.total_earned?.toFixed(2) || 0}</div><div class="cap-label">Ganancia total</div></div>
    <div class="cap-item"><div class="cap-val">${summary.count || 0}/${summary.max_positions || 5}</div><div class="cap-label">Posiciones</div></div>`;

  // Alerts
  document.getElementById('alerts-bar').innerHTML = alerts.map(a => `
    <div class="alert-item ${a.severity === 'WARNING' ? 'warning' : ''}">
      ${a.severity === 'CRITICAL' ? '🚨' : '⚠️'} ${a.symbol} (${a.exchange}): ${a.message}
    </div>`).join('');

  // Play sound for critical alerts
  if (alerts.some(a => a.severity === 'CRITICAL')) playBeep();

  // Positions
  const el = document.getElementById('pos-list');
  if (!positions.length) {
    el.innerHTML = `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M12 10v4m-2-2h4"/></svg>
      <div class="empty-title">Sin posiciones activas</div>
      <div class="empty-sub"><span class="empty-cta" onclick="switchTab('opportunities')">Ve a Oportunidades</span> para abrir una posicion</div>
    </div>`;
    return;
  }

  el.innerHTML = positions.map((p, idx) => {
    const mode = p.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Exchange';
    const exchange = p.mode === 'cross_exchange'
      ? `${p.long_exchange} / ${p.short_exchange}` : p.exchange;
    const frColor = p.current_fr > 0 ? '#22c55e' : '#ef4444';
    const earnColor = p.net_earned >= 0 ? '#22c55e' : '#ef4444';
    const posId = p.id || String(idx);

    let alertHtml = '';
    if (p.fr_reversed) {
      alertHtml = '<div class="pos-alert">FUNDING CAMBIO DE SIGNO — CERRAR POSICION</div>';
    }

    const payments = p.payments || [];
    const lastPayments = payments.slice(-5).reverse();
    let payTable = '';
    if (lastPayments.length) {
      payTable = `
        <div class="pay-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
          Ver ultimos ${lastPayments.length} pagos
        </div>
        <div style="display:none">
          <table class="pay-table">
            <tr><th>#</th><th>Hora</th><th>Tasa</th><th>Ganancia</th><th>Acum</th></tr>
            ${lastPayments.map((pay, i) => `
              <tr>
                <td>${payments.length - i}</td>
                <td>${new Date(pay.ts * 1000).toLocaleTimeString()}</td>
                <td>${(pay.rate * 100).toFixed(4)}%</td>
                <td style="color:${pay.earned >= 0 ? '#22c55e' : '#ef4444'}">$${pay.earned.toFixed(4)}</td>
                <td style="color:${pay.cumulative >= 0 ? '#22c55e' : '#ef4444'}">$${pay.cumulative.toFixed(2)}</td>
              </tr>`).join('')}
          </table>
        </div>`;
    }

    return `
    <div class="pos-card">
      ${alertHtml}
      <div class="pos-header">
        <div>
          <span class="pos-symbol">${p.symbol}/USDT</span>
          <span class="opp-mode">${mode}</span>
          <span style="font-size:11px;color:#888;margin-left:6px">${exchange}</span>
        </div>
        <button class="btn btn-danger" onclick="closePos('${posId}','${p.symbol}')">Cerrar posicion</button>
      </div>

      <div class="pos-grid">
        <div class="pos-field"><span class="label">Capital</span><span class="value">$${p.capital_used.toFixed(0)}</span></div>
        <div class="pos-field"><span class="label">Tiempo</span><span class="value">${p.elapsed_h?.toFixed(1)}h (${(p.elapsed_h/24).toFixed(1)}d)</span></div>
        <div class="pos-field"><span class="label">Pagos recibidos</span><span class="value">${p.payment_count || p.intervals || 0}</span></div>
        <div class="pos-field"><span class="label">Prox pago</span><span class="value">${p.mins_next > 0 ? Math.round(p.mins_next) + 'min' : '—'}</span></div>
        <div class="pos-field"><span class="label">FR entrada</span><span class="value">${(p.entry_fr*100).toFixed(4)}%</span></div>
        <div class="pos-field"><span class="label">FR actual</span><span class="value" style="color:${frColor}">${(p.current_fr*100).toFixed(4)}%</span></div>
        <div class="pos-field"><span class="label">APR actual</span><span class="value" style="color:${frColor}">${p.current_apr?.toFixed(1)}%</span></div>
        <div class="pos-field"><span class="label">Tasa promedio</span><span class="value">${p.avg_rate ? (p.avg_rate*100).toFixed(4) + '%' : '—'}</span></div>
        <div class="pos-field"><span class="label">Ganancia acum</span><span class="value" style="color:#22c55e">$${p.est_earned?.toFixed(2)}</span></div>
        <div class="pos-field"><span class="label">Fees est (E+S)</span><span class="value" style="color:#ef4444">$${p.est_fees_total?.toFixed(2)}</span></div>
        <div class="pos-field"><span class="label">Ganancia neta</span><span class="value" style="color:${earnColor};font-weight:700">$${p.net_earned?.toFixed(2)}</span></div>
      </div>

      ${payTable}
    </div>`;
  }).join('');
}

async function closePos(posId, symbol) {
  if (!await showConfirm(`Cerrar posicion ${symbol}?`)) return;
  try {
    const res = await fetch('/api/close_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ position_id: posId, reason: 'manual' }),
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

let _lastHistoryData = [];

function exportCSV() {
  if (!_lastHistoryData.length) { showToast('Sin historial para exportar', 'warning'); return; }
  const headers = ['Simbolo','Exchange','Modo','Capital','Horas','Pagos','Ganancia','Fees','Neto','Tasa Promedio','Razon','Fecha Cierre'];
  const rows = _lastHistoryData.map(h => [
    h.symbol, h.exchange, h.mode || 'spot_perp',
    h.capital_used?.toFixed(2) || '', h.hours?.toFixed(1) || '',
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
  el.innerHTML = history.slice().reverse().slice(0, 20).map(h => {
    const netColor = (h.net_earned || h.earned) >= 0 ? '#22c55e' : '#ef4444';
    return `
    <div class="hist-item">
      <span>${h.symbol} (${h.exchange}) — ${h.mode || 'spot_perp'}</span>
      <span>${h.hours?.toFixed(1)}h | ${h.payment_count || h.intervals} pagos</span>
      <span style="color:${netColor}">$${(h.net_earned || h.earned)?.toFixed(2)}</span>
      <span style="color:#555">${h.closed_at ? h.closed_at.split('T')[0] : h.time?.split('T')[0] || ''}</span>
    </div>`;
  }).join('');
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
    document.getElementById('cfg-scan-min').value = cfg.scan_minutes;
    document.getElementById('cfg-max-pos').value = cfg.max_positions;
    document.getElementById('cfg-min-vol').value = cfg.min_volume;
    document.getElementById('cfg-min-apr').value = cfg.min_apr;
    document.getElementById('cfg-min-score').value = cfg.min_score;
    document.getElementById('cfg-min-days').value = cfg.min_stability_days;
    document.getElementById('cfg-alert-min').value = cfg.alert_minutes_before;
    document.getElementById('cfg-email-on').checked = cfg.email_enabled;
    document.getElementById('cfg-wa-phone').value = cfg.wa_phone || '';
    document.getElementById('cfg-wa-apikey').value = cfg.wa_apikey || '';
  } catch (e) {
    console.error('loadConfig error:', e);
  }
}

async function saveConfig() {
  const data = {
    total_capital: parseFloat(document.getElementById('cfg-capital').value),
    scan_minutes: parseInt(document.getElementById('cfg-scan-min').value),
    max_positions: parseInt(document.getElementById('cfg-max-pos').value),
    min_volume: parseFloat(document.getElementById('cfg-min-vol').value),
    min_apr: parseFloat(document.getElementById('cfg-min-apr').value),
    min_score: parseInt(document.getElementById('cfg-min-score').value),
    min_stability_days: parseInt(document.getElementById('cfg-min-days').value),
    alert_minutes_before: parseInt(document.getElementById('cfg-alert-min').value),
    email_enabled: document.getElementById('cfg-email-on').checked,
    wa_phone: document.getElementById('cfg-wa-phone').value,
    wa_apikey: document.getElementById('cfg-wa-apikey').value,
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
  if (data.last_scan)
    document.getElementById('st-time').textContent = data.last_scan;
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
          <div style="margin-top:8px;display:flex;gap:8px">
            <button class="btn btn-primary" onclick="saveExchangeKey('${ex}')" style="font-size:11px;padding:4px 12px">Guardar</button>
            ${hasKey ? `<button class="btn btn-danger" onclick="deleteExchangeKey('${ex}')" style="font-size:11px;padding:4px 12px">Eliminar</button>` : ''}
          </div>
        </div>`;
    }).join('');
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
    if (data.ok) window.location.href = '/auth/login';
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
  window.location.href = '/auth/login';
}

// ── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  refresh();
  startRefresh();
  loadExchangeStatus();
  setInterval(loadExchangeStatus, 300000); // every 5 min
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
});
