'use strict';

const API = `http://${location.host}/api`;
let equityChart = null;
let selectedStrategyId = null;
let allResults = {};
let pollingTimer = null;
let resultsTab = 'jp'; // デフォルトはJP株

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('btn-run-all').addEventListener('click', runAll);
  await Promise.all([loadStrategies(), loadResults(), loadPdca(), loadRegime(), loadRegimeAnalysis(), loadReadiness()]);
  startPolling();
  connectWS();
});

// ── WebSocket (受け取るだけ) ───────────────────────────────────────────────────
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = async (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'lab_report') {
      await Promise.all([loadResults(), loadPdca()]);
      updateProgress(0, 0);
      showToast(`✅ サイクル完了 — 最優秀: ${msg.best_strategy} ${fmtJpy(msg.best_daily_jpy)}/日`);
    } else if (msg.type === 'strategy_done') {
      updateProgress(msg.done, msg.total);
      await loadResults();
    }
  };
  ws.onclose = () => setTimeout(connectWS, 5000);
}

function updateProgress(done, total) {
  const wrap  = document.getElementById('progress-bar-wrap');
  const inner = document.getElementById('progress-bar-inner');
  const label = document.getElementById('progress-label');
  if (!wrap) return;
  if (!total) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = 'flex';
  const pct = Math.round((done / total) * 100);
  inner.style.width = pct + '%';
  label.textContent = `${done} / ${total}`;
}

function showToast(text) {
  let el = document.getElementById('lab-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'lab-toast';
    el.style.cssText = `
      position:fixed; bottom:20px; right:20px; z-index:9999;
      background:#0d1a0d; border:1px solid #00ff41; color:#00ff41;
      font-family:'Courier New',monospace; font-size:12px;
      padding:10px 16px; max-width:400px; letter-spacing:0.5px;
      transition: opacity 0.5s;
    `;
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.opacity = '1';
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.style.opacity = '0'; }, 8000);
}

function startPolling() {
  if (pollingTimer) clearInterval(pollingTimer);
  pollingTimer = setInterval(async () => {
    await Promise.all([loadResults(), loadPdca(), loadRegime(), loadRegimeAnalysis(), loadReadiness()]);
  }, 15000);
}

// ── API calls ──────────────────────────────────────────────────────────────────
async function loadStrategies() {
  const res  = await fetch(`${API}/lab/strategies`);
  const data = await res.json();
  renderStrategyList(data.strategies || []);
}

async function loadResults() {
  const res  = await fetch(`${API}/lab/results`);
  const data = await res.json();
  const results = data.results || [];
  const running = data.running || [];
  const progress = data.progress || {};
  results.forEach(r => { allResults[r.strategy_id] = r; });
  renderResultsTable(Object.values(allResults));
  renderRunningList(running);
  updateStrategyStatuses(running);
  if (progress.total > 0 && progress.done < progress.total) {
    updateProgress(progress.done, progress.total);
  }
}

async function loadPdca() {
  const res  = await fetch(`${API}/lab/pdca`);
  const data = await res.json();
  renderPdca(data);
  if (data.screen_results?.length) renderScreenResults(data.screen_results);
}

async function loadRegime() {
  try {
    const res  = await fetch(`${API}/lab/regime`);
    const data = await res.json();
    renderRegime(data.regime || {});
  } catch (e) {}
}

async function loadReadiness() {
  try {
    const res  = await fetch(`${API}/lab/live-readiness`);
    const data = await res.json();
    renderReadiness(data);
  } catch (e) {}
}

async function loadRegimeAnalysis() {
  try {
    const res  = await fetch(`${API}/lab/regime-analysis`);
    const data = await res.json();
    renderRegimeAnalysis(data.regime_analysis || {});
  } catch (e) {}
}

async function runRegimeAnalysisAll() {
  const btn = document.getElementById('btn-regime-analysis');
  btn.disabled = true;
  btn.textContent = '⟳';
  showToast('レジーム分析を開始中... (銘柄ごとに順次実行)');
  try {
    const symbols = ['7203.T','9433.T','8306.T','6758.T','6098.T','6954.T',
                     '2413.T','3697.T','7267.T','6645.T','4568.T','9432.T'];
    for (const sym of symbols) {
      await fetch(`${API}/lab/regime-analysis/${sym}`, { method: 'POST' });
    }
    await loadRegimeAnalysis();
    showToast('✅ レジーム分析完了');
  } catch (e) {
    showToast('⚠ レジーム分析エラー: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '↺ 更新';
  }
}

async function runAll() {
  const btn = document.getElementById('btn-run-all');
  btn.disabled = true;
  btn.textContent = '⟳ RUNNING...';
  const days   = document.getElementById('days-select').value;
  const usdJpy = document.getElementById('usd-jpy-input').value;
  try {
    await fetch(`${API}/lab/run-all?days=${days}&usd_jpy=${usdJpy}`, { method: 'POST' });
    await Promise.all([loadResults(), loadPdca()]);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ RUN ALL';
  }
}

async function runOne(strategyId) {
  const days   = document.getElementById('days-select').value;
  const usdJpy = document.getElementById('usd-jpy-input').value;
  await fetch(`${API}/lab/run/${strategyId}?days=${days}&usd_jpy=${usdJpy}`, { method: 'POST' });
  await Promise.all([loadResults(), loadPdca()]);
}

// ── Render: Strategy list ──────────────────────────────────────────────────────
function renderStrategyList(strategies) {
  const panel = document.getElementById('strategy-list');
  panel.innerHTML = '';
  for (const s of strategies) {
    const r      = allResults[s.id];
    const hasRes = !!r;
    const div    = document.createElement('div');
    div.className = 'strategy-card' + (s.id === selectedStrategyId ? ' active' : '');
    div.innerHTML = `
      <div class="strat-name">${s.name}</div>
      <div class="strat-meta">${s.symbol} · ${s.interval}</div>
      <div class="strat-meta" style="margin-top:1px">${s.description}</div>
      ${hasRes
        ? `<div class="strat-status done">✓ 日次: ${fmtJpy(r.daily_pnl_jpy)} | 勝率: ${r.win_rate.toFixed(1)}%</div>`
        : `<div class="strat-status pending">未実行</div>`
      }
      <button class="btn-run-one" onclick="event.stopPropagation(); runOne('${s.id}')">▶ RUN</button>
    `;
    div.addEventListener('click', () => selectStrategy(s.id));
    panel.appendChild(div);
  }
}

function updateStrategyStatuses(running) {
  document.querySelectorAll('.strategy-card').forEach(card => {
    const id     = card.querySelector('.btn-run-one')?.onclick?.toString().match(/'(\w+)'/)?.[1];
    const status = card.querySelector('.strat-status');
    if (id && status && running.includes(id)) {
      status.className = 'strat-status running';
      status.textContent = '⟳ 実行中...';
    }
  });
}

// ── Tab filter ────────────────────────────────────────────────────────────────
function setResultsTab(tab) {
  resultsTab = tab;
  ['jp', 'btc', 'all'].forEach(t => {
    document.getElementById(`tab-${t}`)?.classList.toggle('active', t === tab);
  });
  renderResultsTable(Object.values(allResults));
}

// ── Render: Results table ──────────────────────────────────────────────────────
function renderResultsTable(results) {
  if (!results.length) return;
  const filtered = results.filter(r => {
    if (resultsTab === 'jp')  return r.symbol?.endsWith('.T');
    if (resultsTab === 'btc') return !r.symbol?.endsWith('.T');
    return true;
  });
  const tbody = document.getElementById('results-tbody');
  const sorted = [...filtered].sort((a, b) => b.score - a.score);
  tbody.innerHTML = '';
  for (const r of sorted) {
    const tr = document.createElement('tr');
    if (r.strategy_id === selectedStrategyId) tr.className = 'selected';
    const dailyJpy = r.daily_pnl_jpy || 0;
    const totalRet = r.total_return_pct || 0;
    const robust   = r.overfitting?.is_robust;
    const robustBadge = robust === false
      ? '<span style="color:#ff3333;font-size:8px;">⚠OVF</span>'
      : robust === true
        ? '<span style="color:#00aa22;font-size:8px;">✓OK</span>'
        : '';
    const avgWin  = r.avg_win_jpy  || 0;
    const avgLoss = r.avg_loss_jpy || 0;
    const rr      = avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : 0;
    const holdMin = fmtHold(r.avg_duration_bars, r.interval);
    tr.innerHTML = `
      <td>${r.strategy_name} ${robustBadge}</td>
      <td class="${r.win_rate >= 55 ? 'td-up' : 'td-dim'}">${r.win_rate.toFixed(1)}%</td>
      <td class="${r.profit_factor >= 1.5 ? 'td-up' : r.profit_factor < 1 ? 'td-down' : 'td-dim'}">${r.profit_factor.toFixed(2)}</td>
      <td class="td-dim">${r.num_trades}</td>
      <td class="${r.max_drawdown_pct > -5 ? 'td-dim' : 'td-down'}">${r.max_drawdown_pct.toFixed(2)}%</td>
      <td class="${dailyJpy >= 1000 ? 'td-up' : dailyJpy < 0 ? 'td-down' : 'td-dim'}">${fmtJpy(dailyJpy)}</td>
      <td class="${avgWin > 0 ? 'td-up' : 'td-dim'}">${fmtJpy(avgWin)}</td>
      <td class="${avgLoss < 0 ? 'td-down' : 'td-dim'}">${fmtJpy(avgLoss)}</td>
      <td class="${rr >= 1.5 ? 'td-up' : rr < 1 ? 'td-down' : 'td-dim'}">${rr.toFixed(2)}</td>
      <td class="td-dim">${holdMin}</td>
      <td class="${r.score > 20 ? 'td-up' : 'td-dim'}">${r.score.toFixed(1)}</td>
    `;
    tr.addEventListener('click', () => selectStrategy(r.strategy_id));
    tbody.appendChild(tr);
  }
}

// ── Select strategy → show equity curve + trades + stats + time patterns ────
function selectStrategy(id) {
  selectedStrategyId = id;
  document.querySelectorAll('.strategy-card').forEach(c => {
    const btn = c.querySelector('.btn-run-one');
    const btnId = btn?.getAttribute('onclick')?.match(/'([^']+)'/)?.[1];
    c.classList.toggle('active', btnId === id);
  });
  document.querySelectorAll('#results-tbody tr').forEach((tr, i) => {
    const name = tr.cells[0]?.textContent;
    const r    = Object.values(allResults).find(r => r.strategy_name === name);
    tr.classList.toggle('selected', r?.strategy_id === id);
  });
  const r = allResults[id];
  if (!r) return;
  renderEquityCurve(r);
  renderStatsDetail(r);
  renderTradeList(r);
  // 時間帯パターン（JP株のみ）
  if (r.symbol?.endsWith('.T')) {
    loadTimePatterns(r.symbol);
  }
}

// ── Equity curve ───────────────────────────────────────────────────────────────
function renderEquityCurve(r) {
  document.getElementById('eq-strategy-name').textContent = r.strategy_name;
  const container = document.getElementById('equity-chart');
  if (equityChart) { equityChart.remove(); equityChart = null; container.innerHTML = ''; }

  const eq = r.equity_curve || [];
  if (eq.length < 2) return;

  const chart = LightweightCharts.createChart(container, {
    width:  container.clientWidth,
    height: 200,
    layout:     { background: { color: '#0d1410' }, textColor: '#4a6a4a', fontFamily: 'Courier New', fontSize: 10 },
    grid:       { vertLines: { color: '#0d1a0d' }, horzLines: { color: '#0d1a0d' } },
    rightPriceScale: { borderColor: '#1a2e1a' },
    timeScale:  { borderColor: '#1a2e1a', timeVisible: false },
  });

  const series = chart.addLineSeries({
    color: eq[eq.length - 1] >= eq[0] ? '#00ff41' : '#ff3333',
    lineWidth: 1.5,
    priceFormat: { type: 'price', precision: 0, minMove: 1 },
  });

  const step = Math.max(1, Math.floor(86400 / eq.length));
  const data = eq.map((v, i) => ({ time: 1700000000 + i * step, value: v }));
  series.setData(data);
  chart.timeScale().fitContent();
  equityChart = chart;
}

// ── Stats Detail (gross profit / avg win / R:R / overfitting) ─────────────────
function renderStatsDetail(r) {
  document.getElementById('stats-strategy-name').textContent = r.strategy_name;
  const panel = document.getElementById('stats-detail');
  const gp  = r.gross_profit_jpy || 0;
  const gl  = r.gross_loss_jpy   || 0;
  const aw  = r.avg_win_jpy      || 0;
  const al  = r.avg_loss_jpy     || 0;
  const rr  = al !== 0 ? Math.abs(aw / al) : 0;
  const rrColor = rr >= 1.5 ? '#00ff41' : rr >= 1.0 ? '#ffcc00' : '#ff3333';

  const ovf = r.overfitting;
  let ovfHtml = '';
  if (ovf) {
    const robustColor = ovf.is_robust ? '#00aa22' : '#ff3333';
    const robustLabel = ovf.is_robust ? '✓ 過学習なし' : '⚠ 過学習疑い';
    const oos  = ovf.oos_ratio  != null ? `OOS/IS: ${ovf.oos_ratio.toFixed(2)}` : '';
    const stab = ovf.stability  != null ? `安定性: ${ovf.stability.toFixed(2)}` : '';
    const pen  = ovf.penalty    != null && ovf.penalty !== 0 ? `ペナルティ: ${ovf.penalty}` : '';
    const warns = (ovf.warnings || []).map(w =>
      `<div style="color:#ffaa00;font-size:9px;margin-top:1px">⚠ ${w}</div>`
    ).join('');
    ovfHtml = `
      <div class="stats-row" style="margin-top:6px;border-top:1px solid #0d1a0d;padding-top:5px;">
        <div style="color:${robustColor};font-weight:bold;font-size:10px;">${robustLabel}</div>
        <div style="color:#4a6a4a;font-size:9px;margin-top:2px;">${[oos, stab, pen].filter(Boolean).join(' | ')}</div>
        ${warns}
      </div>`;
  }

  const spHtml = r.similar_period ? (() => {
    const sp = r.similar_period;
    return `<div class="stats-row" style="margin-top:4px;font-size:9px;color:#4a6a4a;">
      類似期間: ${sp.start}〜${sp.end} (類似度${(sp.similarity*100).toFixed(0)}%)
      <span style="color:#00aa33">${sp.description || sp.regime}</span>
    </div>`;
  })() : '';

  panel.innerHTML = `
    <div class="stats-detail-block">
      <div class="stats-row">
        <span class="stats-label">総利益/日</span>
        <span class="td-up">${fmtJpy(gp)}</span>
        <span class="stats-label" style="margin-left:8px">総損失/日</span>
        <span class="td-down">${fmtJpy(gl)}</span>
      </div>
      <div class="stats-row" style="margin-top:4px">
        <span class="stats-label">平均利益</span>
        <span class="td-up">${fmtJpy(aw)}/回</span>
        <span class="stats-label" style="margin-left:8px">平均損失</span>
        <span class="td-down">${fmtJpy(al)}/回</span>
        <span style="color:${rrColor};font-weight:bold;margin-left:8px">R:R ${rr.toFixed(2)}</span>
      </div>
      ${ovfHtml}
      ${spHtml}
    </div>`;
}

// ── Trade list ─────────────────────────────────────────────────────────────────
function renderTradeList(r) {
  const trades = r.trades || [];
  document.getElementById('trade-count').textContent = `${r.num_trades} trades`;
  const panel = document.getElementById('trade-list');
  panel.innerHTML = '';
  const isJP = r.symbol?.endsWith('.T');
  for (const t of [...trades].reverse().slice(0, 30)) {
    const div = document.createElement('div');
    div.className = 'trade-row';
    // JP株はpnlがすでにJPY建て (usd_jpy=1.0)
    const pnlDisplay = isJP
      ? (t.pnl >= 0 ? `+¥${Math.round(t.pnl).toLocaleString()}` : `-¥${Math.round(Math.abs(t.pnl)).toLocaleString()}`)
      : (t.pnl >= 0 ? `+$${t.pnl.toFixed(2)}` : `-$${Math.abs(t.pnl).toFixed(2)}`);
    div.innerHTML = `
      <span class="tr-time">${t.entry_time?.slice(0, 16)}</span>
      <span class="${t.pnl >= 0 ? 'td-up' : 'td-down'}">${pnlDisplay}</span>
      <span class="td-dim">${t.pnl_pct?.toFixed(3)}%</span>
      <span class="td-dim">${t.duration_bars}本</span>
      <span class="tr-reason">${t.exit_reason}</span>
      <span class="td-dim">${t.side || ''}</span>
    `;
    panel.appendChild(div);
  }
}

// ── PDCA ───────────────────────────────────────────────────────────────────────
function renderPdca(data) {
  document.getElementById('pdca-stage').textContent = data.current_stage;
  const goal = data.goal || {};
  document.getElementById('pdca-goal-text').textContent =
    `Stage ${data.current_stage}: ${goal.description || ''} — 目標 ${fmtJpy(goal.daily_pnl_jpy || 0)}/日`;
  document.getElementById('pdca-next-action').textContent = data.next_action || '';

  const list = document.getElementById('pdca-stages-list');
  list.innerHTML = '';
  for (const s of (data.all_stages || [])) {
    const isCurrent = s.stage === data.current_stage;
    const isDone    = s.stage < data.current_stage;
    const div = document.createElement('div');
    div.className = `pdca-stage-item${isCurrent ? ' current' : ''}${isDone ? ' done' : ''}`;
    div.innerHTML = `
      <div class="pdca-stage-num">${isDone ? '✅' : isCurrent ? '▶' : '○'} Stage ${s.stage}</div>
      <div class="pdca-stage-desc">${s.description}</div>
      <div class="pdca-stage-target">目標: ${fmtJpy(s.daily_pnl_jpy)}/日 · 勝率${s.win_rate}%</div>
    `;
    list.appendChild(div);
  }

  const runList = document.getElementById('running-list');
  const running = [];  // populated by loadResults
  if (!running.length) {
    runList.innerHTML = '<span style="color:#4a6a4a;font-size:10px;">—</span>';
  }
}

function renderRunningList(running) {
  const el = document.getElementById('running-list');
  if (!running.length) {
    el.innerHTML = '<span style="color:#4a6a4a;font-size:10px;">—</span>';
    return;
  }
  el.innerHTML = running.map(id => `<div class="running-item">${id}</div>`).join('');
}

// ── Regime panel (current) ─────────────────────────────────────────────────────
function renderRegime(regime) {
  const panel = document.getElementById('regime-panel');
  if (!panel) return;
  panel.innerHTML = '';
  if (!Object.keys(regime).length) {
    panel.innerHTML = '<div style="padding:6px 10px;font-size:10px;color:#4a6a4a;">判定待機中...</div>';
    return;
  }
  for (const [sym, r] of Object.entries(regime)) {
    const colorMap = {
      trending_up: '#00ff41', trending_down: '#ff3333',
      ranging: '#ffcc00', high_vol: '#ff9900',
      low_vol: '#4a6a4a', unknown: '#4a6a4a',
    };
    const color = colorMap[r.regime] || '#c0e0c0';
    const div = document.createElement('div');
    div.style.cssText = 'padding:6px 10px;border-bottom:1px solid #0d1a0d;';
    div.innerHTML =
      `<div style="font-size:11px;font-weight:bold;color:${color}">${r.regime_jp}</div>` +
      `<div style="font-size:9px;color:#4a6a4a;margin-top:2px">${r.description}</div>` +
      `<div style="font-size:9px;color:#6a9a6a;margin-top:2px">` +
        `ADX ${r.adx} | ATR ${r.atr_pct}% (×${r.atr_vs_avg})` +
      `</div>` +
      (r.recommended?.length
        ? `<div style="font-size:9px;color:#00cc33;margin-top:3px">推奨: ${r.recommended.join(', ')}</div>`
        : `<div style="font-size:9px;color:#cc2200;margin-top:3px">様子見推奨</div>`);
    panel.appendChild(div);
  }
}

// ── Regime Analysis panel (5相場別) ───────────────────────────────────────────
function renderRegimeAnalysis(analysis) {
  const panel = document.getElementById('regime-analysis-panel');
  if (!panel) return;
  const symbols = Object.keys(analysis);
  if (!symbols.length) {
    panel.innerHTML = '<div style="padding:6px 10px;font-size:10px;color:#4a6a4a;">未取得 (毎朝9:15自動更新)</div>';
    return;
  }
  panel.innerHTML = '';
  const regimeJP = {
    uptrend: '上昇', downtrend: '下落', sideways: 'レンジ',
    volatile: '急騰落', calm: '凪',
  };
  for (const sym of symbols) {
    const d = analysis[sym];
    if (!d) continue;
    const best  = d.best_regime  ? (regimeJP[d.best_regime]  || d.best_regime)  : '—';
    const worst = d.worst_regime ? (regimeJP[d.worst_regime] || d.worst_regime) : '—';
    const results = d.results || {};

    const div = document.createElement('div');
    div.className = 'regime-analysis-card';
    // 5相場の棒グラフ行
    const rows = Object.entries(results).map(([regime, r]) => {
      const pnl = r.daily_pnl_jpy || 0;
      const pnlColor = pnl > 0 ? '#00ff41' : '#ff3333';
      const verdict = r.verdict || '';
      const verdictColor = verdict.includes('強') ? '#00ff41' : verdict.includes('弱') ? '#ff3333' : '#ffcc00';
      return `<div class="ra-row">
        <span class="ra-regime">${regimeJP[regime] || regime}</span>
        <span style="color:${pnlColor}">${fmtJpy(pnl)}/日</span>
        <span class="ra-verdict" style="color:${verdictColor}">${verdict.slice(0, 4)}</span>
      </div>`;
    }).join('');

    div.innerHTML = `
      <div class="ra-header">
        <span style="color:#00ff41;font-weight:bold">${sym}</span>
        <span style="color:#4a6a4a;font-size:9px">最強: <span style="color:#00cc33">${best}</span> 最弱: <span style="color:#cc2200">${worst}</span></span>
      </div>
      ${rows}
    `;
    panel.appendChild(div);
  }
}

// ── Time Patterns panel ────────────────────────────────────────────────────────
async function loadTimePatterns(symbol) {
  try {
    const res  = await fetch(`${API}/lab/time-patterns/${symbol}`);
    const data = await res.json();
    renderTimePatterns(data);
  } catch (e) {
    document.getElementById('time-pattern-panel').innerHTML =
      '<div style="padding:6px 10px;font-size:10px;color:#4a6a4a;">データなし</div>';
  }
}

function renderTimePatterns(data) {
  document.getElementById('tp-symbol-label').textContent = data.symbol || '—';
  const panel = document.getElementById('time-pattern-panel');
  const slots = data.slots || [];
  if (!slots.length) {
    panel.innerHTML = '<div style="padding:6px 10px;font-size:10px;color:#4a6a4a;">蓄積データなし</div>';
    return;
  }
  const dangers = new Set((data.zones || []).map(z => `${z.hour}:${z.minute === 0 ? '00' : z.minute}`));
  panel.innerHTML = '';
  for (const s of slots) {
    if (s.n < 3) continue;  // サンプル少なすぎはスキップ
    const timeLabel = `${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}`;
    const isDanger = dangers.has(timeLabel);
    const upPct = (s.up_rate * 100).toFixed(0);
    const upColor = s.up_rate >= 0.6 ? '#00ff41' : s.up_rate <= 0.4 ? '#ff3333' : '#ffcc00';
    const barWidth = Math.round(s.avg_atr_pct * 20);  // ATR%を棒グラフ幅に変換
    const row = document.createElement('div');
    row.className = 'tp-row' + (isDanger ? ' tp-danger' : '');
    row.innerHTML = `
      <span class="tp-time">${timeLabel}</span>
      <div class="tp-bar-wrap">
        <div class="tp-bar" style="width:${Math.min(barWidth, 60)}px;background:${isDanger ? '#ff4400' : '#1a4a1a'}"></div>
      </div>
      <span style="color:${upColor};font-size:9px">↑${upPct}%</span>
      <span class="tp-atr">${s.avg_atr_pct.toFixed(2)}%</span>
      <span class="tp-n">${s.n}本</span>
    `;
    panel.appendChild(row);
  }
}

// ── Live Readiness checklist ──────────────────────────────────────────────────
function renderReadiness(data) {
  const panel = document.getElementById('readiness-panel');
  if (!panel) return;

  const ready    = data.overall_ready;
  const reco     = data.recommendation || '—';
  const blocking = data.blocking_count || 0;
  const recoColor = ready ? '#00ff41' : blocking <= 2 ? '#ffcc00' : '#ff3333';

  // 連続日数カウンター（バックテスト vs ペーパー）
  const btDays    = data.consecutive_bt_days    ?? '—';
  const paperDays = data.consecutive_paper_days ?? '—';
  const btColor   = btDays    >= 5 ? '#00ff41' : btDays    >= 3 ? '#ffcc00' : '#ff3333';
  const ppColor   = paperDays >= 3 ? '#00ff41' : paperDays >= 1 ? '#ffcc00' : '#4a6a4a';

  let html = `<div class="readiness-summary" style="color:${recoColor}">${reco}</div>`;
  html += `<div class="readiness-counters">
    <div class="readiness-counter">
      <div class="rc-label">BT連続+</div>
      <div class="rc-value" style="color:${btColor}">${btDays}日</div>
    </div>
    <div class="readiness-counter">
      <div class="rc-label">ペーパー連続+</div>
      <div class="rc-value" style="color:${ppColor}">${paperDays}日</div>
    </div>
  </div>`;

  for (const c of (data.checklist || [])) {
    const icon  = c.pass ? '✓' : (c.critical ? '✗' : '△');
    const color = c.pass ? '#00aa22' : (c.critical ? '#ff3333' : '#ffaa00');
    html += `<div class="readiness-row">
      <span style="color:${color};width:14px;display:inline-block">${icon}</span>
      <span class="readiness-item ${c.pass ? '' : (c.critical ? 'readiness-fail' : 'readiness-warn')}">${c.item}</span>
      <span class="readiness-value" style="color:${c.pass ? '#4a6a4a' : color}">${c.value}</span>
    </div>`;
  }

  if (data.best_strategy && data.best_strategy !== '—') {
    html += `<div style="padding:4px 8px;font-size:9px;color:#4a6a4a;border-top:1px solid #0d1a0d;margin-top:2px">
      最優秀: ${data.best_strategy}
    </div>`;
  }

  panel.innerHTML = html;
}

// ── Screen results ────────────────────────────────────────────────────────────
function renderScreenResults(results) {
  const panel = document.getElementById('screen-list');
  if (!panel) return;
  panel.innerHTML = '';
  const sorted = [...results].sort((a, b) => b.score - a.score);
  for (const r of sorted) {
    const div = document.createElement('div');
    div.style.cssText = 'padding:4px 10px;border-bottom:1px solid #0d1a0d;font-size:10px;';
    const star = r.selected ? '<span style="color:#ffcc00">★</span> ' : '  ';
    div.innerHTML =
      `${star}<span style="color:${r.selected?'#00ff41':'#4a6a4a'}">${r.symbol}</span> ` +
      `<span style="color:#c0e0c0">${r.name}</span><br>` +
      `<span style="color:#4a6a4a">ATR ${r.avg_atr_pct?.toFixed(2)}% ` +
      `出来高${Math.round(r.avg_volume||0)}千株 スコア${r.score?.toFixed(1)}</span>`;
    panel.appendChild(div);
  }
}

// ── Formatters ─────────────────────────────────────────────────────────────────
function fmtJpy(v) {
  if (v == null) return '—';
  const sign = v >= 0 ? '+' : '';
  return sign + Math.round(v).toLocaleString('ja-JP') + '円';
}

function fmtHold(bars, interval) {
  if (!bars) return '—';
  const mins = {'1m':1,'5m':5,'15m':15,'1h':60}[interval] || 5;
  const total = Math.round(bars * mins);
  if (total < 60) return total + '分';
  return Math.round(total / 60 * 10) / 10 + '時間';
}
