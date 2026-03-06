let lastErr='';
let currentTab='dashboard';

function switchTab(tab){
  currentTab=tab;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  event.target.classList.add('active');
  if(tab==='opportunities')loadOpportunities();
  else ld();
}

function exBadge(exch){
  const m={'Binance':'bbn','Bybit':'bbb','OKX':'bokx','Bitget':'bbg'};
  return `<span class="badge ${m[exch]||'bgy'}">${exch}</span>`;
}

// ========== DASHBOARD ==========
async function ld(){
try{const r=await fetch('/api/state');if(!r.ok)throw new Error('HTTP '+r.status);
const s=await r.json();
if(currentTab==='dashboard')renderDashboard(s);
else if(currentTab==='positions')renderPositions(s);
lastErr=''}
catch(e){if(lastErr!==e.message){lastErr=e.message;
document.getElementById('ct').innerHTML=`<div class="err">Error: ${e.message}</div>`}}}

function renderDashboard(s){
const bd=s.breakdown;let h='';
h+=`<div class="cap"><div class="i"><div class="l">Capital</div><div class="v">$${s.capital.toLocaleString()}</div></div>
<div class="i"><div class="l">Ganado</div><div class="v g">$${s.earned.toFixed(2)}</div></div>
<div class="i"><div class="l">En Uso</div><div class="v">$${(bd.su+bd.au).toFixed(0)}</div></div></div>`;
h+=`<div class="bd"><div class="bdi sf"><div class="t">🛡️ Seguro (${bd.sc} pos)</div><div class="vl">$${bd.sb.toFixed(0)}</div><div class="sb">Libre: $${bd.sa.toFixed(0)}</div></div>
<div class="bdi ag"><div class="t">⚡ Agresivo (${bd.ac} pos)</div><div class="vl">$${bd.ab.toFixed(0)}</div><div class="sb">Libre: $${bd.aa.toFixed(0)}</div></div></div>`;
if(s.last_error)h+=`<div class="err">${s.last_error}</div>`;

// POSITIONS summary
if(s.positions.length>0){
h+='<div class="st">📊 Posiciones Activas</div>';
s.positions.forEach((p,pi)=>{
const e=p.carry==='Positive'?'🛡️':'⚡';const bc=p.carry==='Positive'?'bsf':'bag';
const cd=p.mins_next>0?`⏱${Math.floor(p.mins_next)}m`:'';
const slInfo=p.sl_pct>0?` | SL:-${p.sl_pct.toFixed(2)}%`:'';
h+=`<div class="pc ${p.fr_reversed||p.sl_hit?'al':''}">
<div class="ph"><span>${e}</span><span class="ps">${p.symbol}</span>
${exBadge(p.exchange)}
<span style="font-size:.57em;color:#555;margin-left:auto">$${p.capital_used.toFixed(0)} | ${p.elapsed_h.toFixed(1)}h | ${p.intervals}cobros ${cd}${slInfo}</span></div>
${p.fr_reversed?'<div class="alrt">⛔ FUNDING CAMBIO — CERRAR</div>':''}
${p.sl_hit?'<div class="alrt">⛔ STOP LOSS ALCANZADO — CERRAR</div>':''}
<div class="pg">
<div class="pm"><div class="pl">FR</div><div class="pv ${p.current_fr>0?'g':'r'}">${(p.current_fr*100).toFixed(4)}%</div></div>
<div class="pm"><div class="pl">APR</div><div class="pv ${p.current_apr>10?'g':'y'}">${p.current_apr.toFixed(1)}%</div></div>
<div class="pm"><div class="pl">Ganado</div><div class="pv g">$${p.est_earned.toFixed(2)}</div></div>
${p.carry==='Reverse'?`<div class="pm"><div class="pl">P&L Precio</div><div class="pv ${p.price_pnl>=0?'g':'r'}">$${p.price_pnl.toFixed(2)}</div></div>`:''}
<div class="pm"><div class="pl">Total</div><div class="pv ${p.total_pnl>=0?'g':'r'}">$${p.total_pnl.toFixed(2)}</div></div>
</div>
<button class="btn br" style="margin-top:6px;font-size:.62em" onclick="mc(${pi})">✖ Cerrar manual</button>
</div>`})}

// ACTIONS
if(s.actions.length>0){
h+='<div class="st">🎯 Acciones</div>';
s.actions.forEach((a,i)=>{
let cc=a.critical?'cr':(a.carry==='safe'?'so':(a.carry==='aggr'?'ao':''));
h+=`<div class="ac ${cc}"><div class="at">${a.title}</div><div class="ad">${a.detail}</div>`;
if(a.steps&&a.steps.length)h+=`<div class="as">${a.steps.join('\n')}</div>`;
if(a.costs)h+=`<div class="acs">${a.costs}</div>`;
if(a.warning)h+=`<div class="aw">${a.warning}</div>`;
if(a.countdown)h+=`<div class="acd">${a.countdown}</div>`;
if(a.type==='OPEN')h+=`<button class="btn bg" onclick="cf(${i})">✅ Ya ejecute</button> <button class="btn bgy" onclick="sk('${a.symbol}','${a.exchange}')">⏭ Siguiente</button>`;
else if(a.type==='EXIT')h+=`<button class="btn br" onclick="cf(${i})">⛔ Ya cerre</button>`;
else if(a.type==='ROTATE')h+=`<button class="btn by" onclick="cf(${i})">🔄 Ya rote</button>`;
h+='</div>'})}

// TOP MARKET
if(s.safe_top.length||s.aggr_top.length){
h+='<div class="st">📈 Top Mercado</div><div class="tr">';
if(s.safe_top.length){h+='<div><div style="font-size:.62em;color:#34d399;margin-bottom:3px">🛡️ Seguras (spot+perp)</div>';
s.safe_top.forEach(o=>{const t=o.token;
h+=`<div class="om"><span class="os">${t.symbol}</span> ${exBadge(t.exchange)}
<span class="of">+${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%</span></div>`});h+='</div>'}
if(s.aggr_top.length){h+='<div><div style="font-size:.62em;color:#fbbf24;margin-bottom:3px">⚡ Agresivas (RSI≤40)</div>';
s.aggr_top.forEach(o=>{const t=o.token;const rsi=o.rsi>=0?` RSI:${o.rsi.toFixed(0)}`:'';
h+=`<div class="om"><span class="os">${t.symbol}</span> ${exBadge(t.exchange)}
<span class="of n">${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%${rsi}</span></div>`});h+='</div>'}
h+='</div>'}

h+=`<div class="scr"><button class="btn bgy" onclick="fs()">🔍 Escanear</button> <button class="btn bgy" onclick="showCfg()">⚙️ Config</button> <button class="btn bgy" onclick="clsk()">🔄 Limpiar descartados</button></div>`;
h+=`<div id="cfgPanel" style="display:none"></div>`;
h+=`<div class="sbar">${s.status} | #${s.scan_count} | ${s.last_scan} | Cada ${Math.floor(s.scan_interval/60)}min | Auto 30s</div>`;
document.getElementById('ct').innerHTML=h;
if(s.actions.some(a=>a.critical)){try{new Audio('data:audio/wav;base64,UklGRl9vAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQ==').play()}catch(e){}}}

// ========== OPPORTUNITIES TAB ==========
async function loadOpportunities(){
try{
const r=await fetch('/api/opportunities');if(!r.ok)throw new Error('HTTP '+r.status);
const d=await r.json();renderOpportunities(d)}
catch(e){document.getElementById('ct').innerHTML=`<div class="err">Error: ${e.message}</div>`}}

function renderOpportunities(d){
let h='';

// Spot-Perp opportunities
if(d.spot_perp&&d.spot_perp.length){
h+=`<div class="st"><span class="opp-mode sp">SPOT-PERP</span> Oportunidades (${d.spot_perp.length})</div>`;
h+=`<table class="opp-tbl"><tr><th>#</th><th>Symbol</th><th>Exchange</th><th>Rate</th><th>3D Acum</th><th>APR</th><th>$/dia</th><th>Net 3D</th><th>Fees</th><th>BE(h)</th><th>Score</th></tr>`;
d.spot_perp.forEach((o,i)=>{
const rateClass=o.funding_rate>0?'g':'r';
h+=`<tr>
<td>${i+1}</td>
<td><strong>${o.symbol}</strong></td>
<td>${exBadge(o.exchange)}</td>
<td class="${rateClass}">${(o.funding_rate*100).toFixed(4)}%/${o.interval_hours}h</td>
<td class="g">${o.accumulated_3d_pct.toFixed(3)}%</td>
<td class="g">${o.apr.toFixed(1)}%</td>
<td>$${o.daily_income_per_1000.toFixed(2)}</td>
<td class="${o.net_3d_revenue_per_1000>0?'g':'r'}">$${o.net_3d_revenue_per_1000.toFixed(2)}</td>
<td>$${o.fees_total.toFixed(2)}</td>
<td>${o.break_even_hours.toFixed(0)}</td>
<td>${o.score}</td>
</tr>`});
h+=`</table>`}

// Cross-Exchange opportunities
if(d.cross_exchange&&d.cross_exchange.length){
h+=`<div class="st" style="margin-top:12px"><span class="opp-mode cx">CROSS-EXCHANGE</span> Oportunidades (${d.cross_exchange.length})</div>`;
h+=`<table class="opp-tbl"><tr><th>#</th><th>Symbol</th><th>Long</th><th>Short</th><th>Diff</th><th>3D Acum</th><th>APR</th><th>Net 3D</th><th>Risk</th><th>Score</th></tr>`;
d.cross_exchange.forEach((o,i)=>{
h+=`<tr>
<td>${i+1}</td>
<td><strong>${o.symbol}</strong></td>
<td>${exBadge(o.long_exchange)} <span class="r">${(o.long_rate*100).toFixed(4)}%</span></td>
<td>${exBadge(o.short_exchange)} <span class="g">${(o.short_rate*100).toFixed(4)}%</span></td>
<td class="g">${(o.rate_differential*100).toFixed(4)}%</td>
<td class="g">${o.accumulated_3d_pct.toFixed(3)}%</td>
<td class="g">${o.apr.toFixed(1)}%</td>
<td class="${o.net_3d_revenue_per_1000>0?'g':'r'}">$${o.net_3d_revenue_per_1000.toFixed(2)}</td>
<td class="${o.liquidation_risk==='LOW'?'g':(o.liquidation_risk==='HIGH'?'r':'y')}">${o.liquidation_risk}</td>
<td>${o.score}</td>
</tr>`});
h+=`</table>`}

// Coinglass data
if(d.coinglass&&d.coinglass.length){
h+=`<div class="st" style="margin-top:12px">🔮 Coinglass Data (${d.coinglass.length})</div>`;
d.coinglass.slice(0,10).forEach(o=>{
h+=`<div class="om"><span class="os">${o.symbol}</span> <span style="color:#666">APR: ${o.apr||'—'}% | OI: $${((o.open_interest||0)/1e6).toFixed(1)}M</span></div>`})}

if(!d.spot_perp?.length&&!d.cross_exchange?.length){
h+=`<div class="ld" style="padding:30px">Sin oportunidades detectadas. Esperando proximo scan...</div>`}

h+=`<div class="sbar">Ultimo scan: ${d.last_scan} | #${d.scan_count} | Per $1,000 notional</div>`;
document.getElementById('ct').innerHTML=h}

// ========== POSITIONS TAB ==========
function renderPositions(s){
let h='';
if(!s.positions.length){
h+=`<div class="ld" style="padding:30px">No hay posiciones activas</div>`;
document.getElementById('ct').innerHTML=h;return}

h+='<div class="st">💼 Posiciones Activas</div>';
s.positions.forEach((p,pi)=>{
const e=p.carry==='Positive'?'🛡️':'⚡';
h+=`<div class="pc ${p.fr_reversed||p.sl_hit?'al':''}">
<div class="ph"><span>${e}</span><span class="ps">${p.symbol}</span>
${exBadge(p.exchange)}
<span class="badge ${p.carry==='Positive'?'bsf':'bag'}">${p.mode||'spot_perp'}</span>
</div>
${p.fr_reversed?'<div class="alrt">⛔ FUNDING CAMBIO</div>':''}
${p.sl_hit?'<div class="alrt">⛔ STOP LOSS</div>':''}
<div class="pg" style="grid-template-columns:repeat(3,1fr)">
<div class="pm"><div class="pl">Capital</div><div class="pv">$${p.capital_used.toFixed(0)}</div></div>
<div class="pm"><div class="pl">Tiempo</div><div class="pv">${p.elapsed_h.toFixed(1)}h</div></div>
<div class="pm"><div class="pl">Cobros</div><div class="pv">${p.intervals}</div></div>
<div class="pm"><div class="pl">Entry FR</div><div class="pv">${(p.entry_fr*100).toFixed(4)}%</div></div>
<div class="pm"><div class="pl">Current FR</div><div class="pv ${p.current_fr>0?'g':'r'}">${(p.current_fr*100).toFixed(4)}%</div></div>
<div class="pm"><div class="pl">APR</div><div class="pv ${p.current_apr>10?'g':'y'}">${p.current_apr.toFixed(1)}%</div></div>
<div class="pm"><div class="pl">Funding Ganado</div><div class="pv g">$${p.est_earned.toFixed(3)}</div></div>
<div class="pm"><div class="pl">P&L Precio</div><div class="pv ${p.price_pnl>=0?'g':'r'}">$${p.price_pnl.toFixed(2)}</div></div>
<div class="pm"><div class="pl">Total P&L</div><div class="pv ${p.total_pnl>=0?'g':'r'}">$${p.total_pnl.toFixed(3)}</div></div>
</div>
<button class="btn br" style="margin-top:8px" onclick="mc(${pi})">✖ Cerrar posicion</button>
</div>`});

h+=`<div style="text-align:center;padding:10px 0;font-size:.65em;color:#555">
Total ganado historico: <span class="g">$${s.earned.toFixed(2)}</span>
</div>`;
document.getElementById('ct').innerHTML=h}

// ========== ACTIONS ==========
async function cf(i){const b=event.target;b.disabled=true;b.textContent='⏳...';
try{const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_idx:i})});
const d=await r.json();alert(d.msg);ld()}catch(e){alert('Error: '+e.message)}b.disabled=false}

async function mc(i){
if(!confirm('¿Cerrar esta posicion manualmente?'))return;
try{const r=await fetch('/api/manual_close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position_idx:i})});
const d=await r.json();alert(d.msg);ld()}catch(e){alert('Error: '+e.message)}}

async function sk(sym,exch){
try{const r=await fetch('/api/skip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,exchange:exch})});
const d=await r.json();ld()}catch(e){alert('Error: '+e.message)}}

async function clsk(){try{await fetch('/api/clear_skips',{method:'POST'});ld()}catch(e){}}

async function fs(){try{await fetch('/api/force_scan',{method:'POST'});document.getElementById('ct').innerHTML='<div class="ld"><div class="sp"></div><br>Escaneando...</div>';setTimeout(ld,6000)}catch(e){}}

async function showCfg(){
const p=document.getElementById('cfgPanel');
if(p&&p.style.display!=='none'){p.style.display='none';return}
try{const r=await fetch('/api/config');const c=await r.json();
if(!p)return;
p.innerHTML=`<div class="ac" style="margin-top:6px">
<div class="at">⚙️ Configuracion</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;font-size:.68em">
<label>Capital USD<br><input id="cc" type="number" value="${c.capital}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Scan min<br><input id="csm" type="number" value="${c.scan_minutes}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>% Seguro<br><input id="csp" type="number" value="${c.safe_pct}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Vol min<br><input id="cmv" type="number" value="${c.min_volume}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>APR min safe<br><input id="cas" type="number" value="${c.min_apr_safe}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>APR min aggr<br><input id="caa" type="number" value="${c.min_apr_aggr}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Score min<br><input id="cms" type="number" value="${c.min_score}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Max pos safe<br><input id="cps" type="number" value="${c.max_pos_safe}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
</div>
<div style="margin-top:8px"><button class="btn bg" onclick="saveCfg()">💾 Guardar</button> <button class="btn bgy" onclick="document.getElementById('cfgPanel').style.display='none'">Cerrar</button></div></div>`;
p.style.display='block'}catch(e){alert('Error: '+e.message)}}

async function saveCfg(){
const body={capital:+document.getElementById('cc').value,scan_minutes:+document.getElementById('csm').value,
safe_pct:+document.getElementById('csp').value,min_volume:+document.getElementById('cmv').value,
min_apr_safe:+document.getElementById('cas').value,min_apr_aggr:+document.getElementById('caa').value,
min_score:+document.getElementById('cms').value,max_pos_safe:+document.getElementById('cps').value};
try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const d=await r.json();alert(d.msg);document.getElementById('cfgPanel').style.display='none';ld()}catch(e){alert('Error: '+e.message)}}

ld();setInterval(()=>{if(currentTab==='opportunities')loadOpportunities();else ld()},30000);
