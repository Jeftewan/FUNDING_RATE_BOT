let currentTab = 'opportunities';
let refreshTimer = null;

// ── Tab switching ─────────────────────────────────────────────
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
  refresh();
}

function refresh() {
  if (currentTab === 'opportunities') loadOpps();
  else if (currentTab === 'positions') loadPositions();
  else if (currentTab === 'config') loadConfig();
}

// ── Auto refresh ──────────────────────────────────────────────
function startRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refresh, 30000);
}

// ── Opportunities ─────────────────────────────────────────────
async function loadOpps() {
  try {
    const res = await fetch('/api/opportunities');
    const data = await res.json();
    renderOpps(data);
    updateStatus(data);
  } catch (e) {
    console.error('loadOpps error:', e);
  }
}

function renderOpps(data) {
  const opps = data.opportunities || [];
  const el = document.getElementById('opp-list');
  document.getElementById('opp-count').textContent =
    `${opps.length} oportunidades (${data.total_unfiltered || 0} total)`;

  if (!opps.length) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555">Sin oportunidades — esperando escaneo</div>';
    return;
  }

  el.innerHTML = opps.map((o, i) => {
    const mode = o.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Exchange';
    const exchange = o.mode === 'spot_perp' ? o.exchange :
      `${o.long_exchange} / ${o.short_exchange}`;
    const grade = o.stability_grade || gradeFromScore(o.score);
    const fr = o.mode === 'spot_perp' ? o.funding_rate : o.rate_differential;
    const frPct = (fr * 100).toFixed(4);
    const days = o.estimated_hold_days || '?';

    return `
    <div class="opp-card">
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
        <div class="opp-stat"><span class="label">FR</span><span class="value green">${frPct}%</span></div>
        <div class="opp-stat"><span class="label">APR</span><span class="value green">${o.apr?.toFixed(1)}%</span></div>
        <div class="opp-stat"><span class="label">3d Acum</span><span class="value">${o.accumulated_3d_pct?.toFixed(3)}%</span></div>
        <div class="opp-stat"><span class="label">$/dia (1K)</span><span class="value blue">$${o.daily_income_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Neto 3d (1K)</span><span class="value blue">$${o.net_3d_revenue_per_1000?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Fees</span><span class="value">$${o.fees_total?.toFixed(2) || o.total_fees?.toFixed(2)}</span></div>
        <div class="opp-stat"><span class="label">Break-even</span><span class="value">${o.break_even_hours?.toFixed(1)}h</span></div>
        <div class="opp-stat"><span class="label">Est. dias</span><span class="value">${days}d</span></div>
      </div>

      <div class="opp-meta">
        Vol: $${fmtVol(o.volume_24h)} |
        Intervalo: ${o.interval_hours || '?'}h |
        ${o.mins_to_next > 0 ? `Prox pago: ${Math.round(o.mins_to_next)}min` : ''}
      </div>

      <div class="opp-actions">
        <input type="number" id="cap-${i}" placeholder="Capital $" class="inp-sm" style="width:100px">
        <button class="btn btn-calc" onclick="calcEst('${o._id}',${i})">Calcular</button>
        <button class="btn btn-enter" onclick="enterPosition('${o._id}',${i})">Entrar</button>
        <span class="opp-est" id="est-${i}"></span>
      </div>
    </div>`;
  }).join('');
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
  if (!cap || cap <= 0) { alert('Ingresa capital'); return; }

  const el = document.getElementById('est-' + idx);
  el.textContent = 'Calculando...';

  try {
    const res = await fetch('/api/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opportunity_id: oppId, capital: cap }),
    });
    const data = await res.json();
    if (data.ok) {
      const e = data.estimate;
      el.innerHTML = `
        <span style="color:#22c55e">$${e.daily_income.toFixed(2)}/dia</span> |
        <span style="color:#22c55e">$${e.income_3day.toFixed(2)}/3d</span> |
        Neto: <span style="color:#fff">$${e.net_3day.toFixed(2)}</span> |
        Fees: $${e.fees_total.toFixed(2)} |
        BE: ${e.break_even_hours.toFixed(1)}h`;
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
  if (!cap || cap <= 0) { alert('Ingresa capital primero'); return; }
  if (!confirm(`Abrir posicion con $${cap}?`)) return;

  try {
    const res = await fetch('/api/open_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opportunity_id: oppId, capital: cap }),
    });
    const data = await res.json();
    if (data.ok) {
      showStepsModal(data);
      loadOpps();
    } else {
      alert(data.msg);
    }
  } catch (e) {
    alert('Error al abrir posicion');
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
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555">Sin posiciones activas</div>';
    return;
  }

  el.innerHTML = positions.map(p => {
    const mode = p.mode === 'spot_perp' ? 'Spot-Perp' : 'Cross-Exchange';
    const exchange = p.mode === 'cross_exchange'
      ? `${p.long_exchange} / ${p.short_exchange}` : p.exchange;
    const frColor = p.current_fr > 0 ? '#22c55e' : '#ef4444';
    const earnColor = p.net_earned >= 0 ? '#22c55e' : '#ef4444';

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
                <td style="color:#22c55e">$${pay.earned.toFixed(4)}</td>
                <td>$${pay.cumulative.toFixed(2)}</td>
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
        <button class="btn btn-danger" onclick="closePos('${p.id}','${p.symbol}')">Cerrar posicion</button>
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
  if (!confirm(`Cerrar posicion ${symbol}?`)) return;
  try {
    const res = await fetch('/api/close_position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ position_id: posId, reason: 'manual' }),
    });
    const data = await res.json();
    if (data.ok) {
      const r = data.result;
      alert(`${r.symbol} cerrada\nGanancia: $${r.earned.toFixed(2)}\nFees: $${r.fees.toFixed(2)}\nNeto: $${r.net_earned.toFixed(2)}\nDuracion: ${r.hours.toFixed(1)}h`);
      loadPositions();
    } else {
      alert(data.msg);
    }
  } catch (e) {
    alert('Error al cerrar');
  }
}

function renderHistory(data) {
  const history = data.history || [];
  const el = document.getElementById('history-list');
  if (!history.length) {
    el.innerHTML = '<div style="font-size:11px;color:#555">Sin historial</div>';
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
    document.getElementById('cfg-smtp-host').value = cfg.smtp_host;
    document.getElementById('cfg-smtp-port').value = cfg.smtp_port;
    document.getElementById('cfg-smtp-user').value = cfg.smtp_user;
    document.getElementById('cfg-smtp-pass').value = cfg.smtp_password === '***' ? '' : cfg.smtp_password;
    document.getElementById('cfg-email-to').value = cfg.email_to;
  } catch (e) {
    console.error('loadConfig error:', e);
  }
}

async function saveConfig() {
  const pass = document.getElementById('cfg-smtp-pass').value;
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
    smtp_host: document.getElementById('cfg-smtp-host').value,
    smtp_port: parseInt(document.getElementById('cfg-smtp-port').value),
    smtp_user: document.getElementById('cfg-smtp-user').value,
    email_to: document.getElementById('cfg-email-to').value,
  };
  if (pass) data.smtp_password = pass;

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
  try {
    const res = await fetch('/api/test_email', { method: 'POST' });
    const data = await res.json();
    document.getElementById('email-status').textContent = data.msg;
    document.getElementById('email-status').style.color = data.ok ? '#22c55e' : '#ef4444';
  } catch (e) {
    document.getElementById('email-status').textContent = 'Error';
    document.getElementById('email-status').style.color = '#ef4444';
  }
}

// ── Force scan ────────────────────────────────────────────────
async function forceScan() {
  try {
    await fetch('/api/force_scan', { method: 'POST' });
    document.getElementById('st-status').textContent = 'Escaneando...';
    setTimeout(refresh, 5000);
  } catch (e) {}
}

// ── Status bar ────────────────────────────────────────────────
function updateStatus(data) {
  if (data.status) document.getElementById('st-status').textContent = data.status;
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

// ── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  refresh();
  startRefresh();
});
