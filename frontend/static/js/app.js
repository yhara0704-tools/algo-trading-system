/**
 * Algo Trading Terminal — Frontend
 * Chart.js + WebSocket real-time updates
 */

'use strict';

// ── Constants ──────────────────────────────────────────────────────────────────
const WS_URL = `ws://${location.host}/ws`;
const API = `http://${location.host}/api`;
const MAX_LOG = 200;
const MAX_SPREAD_HISTORY = 120;

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  ws: null,
  reconnectTimer: null,
  selectedSymbol: 'BTC-USD',
  prices: {},           // symbol → latest price data
  spreadHistory: [],    // [{ts, spread_pct, signal}]
  candles: [],          // current symbol OHLCV
  polymarkets: [],      // polymarket markets
  logs: [],             // event log
  charts: {
    main: null,
    spread: null,
  },
  chartInterval: '5m',
  trading: {
    account:   { cash: 100000, equity: 100000, starting_cash: 100000 },
    positions: [],
    orders:    [],
  },
};

// ── Catalogue (from server, cached) ───────────────────────────────────────────
let catalogue = {};

// ── WebSocket ──────────────────────────────────────────────────────────────────
function connectWS() {
  setConnStatus('connecting');
  const ws = new WebSocket(WS_URL);
  state.ws = ws;

  ws.onopen = () => {
    setConnStatus('connected');
    log('info', 'WebSocket connected');
    clearTimeout(state.reconnectTimer);
    // Initial OHLCV load
    loadOhlcv(state.selectedSymbol);
    loadPolymarkets();
  };

  ws.onmessage = (evt) => {
    try {
      handleMessage(JSON.parse(evt.data));
    } catch (e) {
      console.error('WS parse error', e);
    }
  };

  ws.onclose = () => {
    setConnStatus('disconnected');
    log('warn', 'WebSocket disconnected — reconnecting in 5s');
    state.reconnectTimer = setTimeout(connectWS, 5000);
  };

  ws.onerror = (e) => {
    log('error', 'WebSocket error');
  };
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'init':        handleInit(msg); break;
    case 'tick':        handleTick(msg); break;
    case 'spread':      handleSpread(msg); break;
    case 'polymarket':  handlePolymarket(msg); break;
    case 'ohlcv':       handleOhlcv(msg); break;
    case 'trade_update': handleTradeUpdate(msg); break;
    case 'lab_report':   handleLabReport(msg); break;
    case 'ping': break;
  }
}

function handleInit(msg) {
  // Merge all price data
  if (msg.coinbase) {
    Object.assign(state.prices, msg.coinbase);
  }
  if (msg.multi) {
    Object.assign(state.prices, msg.multi);
  }
  if (msg.spread) {
    onSpreadUpdate(msg.spread);
  }
  renderWatchlist();
  updateQuoteBar(state.selectedSymbol);
}

function handleTick(data) {
  const prev = state.prices[data.symbol];
  state.prices[data.symbol] = data;
  updateTickerTape(data);
  updateWatchlistRow(data, prev);
  if (data.symbol === state.selectedSymbol) {
    updateQuoteBar(data.symbol, prev);
  }
}

function handleSpread(data) {
  onSpreadUpdate(data);
}

function handlePolymarket(data) {
  if (data.markets) {
    state.polymarkets = data.markets;
    renderPolymarkets();
  }
}

function handleOhlcv(msg) {
  if (msg.symbol === state.selectedSymbol) {
    state.candles = msg.candles || [];
    renderMainChart();
  }
}

// ── REST API ───────────────────────────────────────────────────────────────────
async function loadCatalogue() {
  try {
    const res = await fetch(`${API}/assets`);
    const data = await res.json();
    catalogue = data.assets || {};
    renderWatchlist();
  } catch (e) {
    log('error', 'Failed to load asset catalogue');
  }
}

async function loadOhlcv(symbol) {
  const interval = state.chartInterval;
  const periodMap = { '1m': '1d', '5m': '5d', '15m': '1mo', '1h': '1mo', '1d': '1mo' };
  const period = periodMap[interval] || '5d';
  try {
    const res = await fetch(`${API}/ohlcv/${encodeURIComponent(symbol)}?period=${period}&interval=${interval}`);
    const data = await res.json();
    state.candles = data.candles || [];
    renderMainChart();
    log('info', `Loaded ${state.candles.length} candles for ${symbol}`);
  } catch (e) {
    log('warn', `OHLCV load failed: ${symbol}`);
  }
}

async function loadPolymarkets() {
  try {
    const res = await fetch(`${API}/polymarket/markets`);
    const data = await res.json();
    state.polymarkets = data.markets || [];
    const latest = data.latest || {};
    if (latest.implied_btc) {
      log('signal', `Polymarket implied BTC: $${fmt(latest.implied_btc)}`);
    }
    renderPolymarkets();
  } catch (e) {
    log('warn', 'Polymarket data unavailable');
  }
}

async function loadSpreadHistory() {
  try {
    const res = await fetch(`${API}/spread`);
    const data = await res.json();
    if (data.history) {
      state.spreadHistory = data.history.slice(-MAX_SPREAD_HISTORY);
      renderSpreadChart();
    }
    if (data.latest) onSpreadUpdate(data.latest);
  } catch (e) {}
}

// ── Spread ─────────────────────────────────────────────────────────────────────
function onSpreadUpdate(snap) {
  if (!snap) return;
  state.spreadHistory.push(snap);
  if (state.spreadHistory.length > MAX_SPREAD_HISTORY) {
    state.spreadHistory.shift();
  }
  renderSpreadStats(snap);
  renderSpreadChart();
  if (snap.signal && snap.signal !== 'neutral' && snap.confidence > 0.4) {
    const dir = snap.signal === 'long' ? '▲ LONG' : '▼ SHORT';
    log('signal', `SIGNAL: ${dir} (conf=${(snap.confidence*100).toFixed(0)}%) spread=${snap.spread_pct?.toFixed(3)}%`);
  }
}

// ── Render: Watchlist ──────────────────────────────────────────────────────────
function renderWatchlist() {
  const panel = document.getElementById('watchlist');
  panel.innerHTML = '';

  const categories = {
    crypto:   'CRYPTO',
    fx:       'FX / FOREX',
    futures:  'FUTURES',
    jp_stock: 'JP STOCKS',
    index:    'INDICES',
    us_stock: 'US STOCKS',
  };

  const grouped = {};
  const allSymbols = new Set([...Object.keys(catalogue), ...Object.keys(state.prices)]);

  for (const sym of allSymbols) {
    const meta = catalogue[sym] || {};
    const cat = meta.category || 'other';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(sym);
  }

  for (const [cat, label] of Object.entries(categories)) {
    const syms = grouped[cat];
    if (!syms || syms.length === 0) continue;

    const header = document.createElement('div');
    header.className = 'category-header';
    header.textContent = label;
    panel.appendChild(header);

    for (const sym of syms.sort()) {
      panel.appendChild(buildAssetRow(sym));
    }
  }
}

function buildAssetRow(sym) {
  const data = state.prices[sym] || {};
  const meta = catalogue[sym] || {};
  const price = data.price;
  const chg = data.change_pct ?? data.change_24h;

  const row = document.createElement('div');
  row.className = 'asset-row' + (sym === state.selectedSymbol ? ' active' : '');
  row.dataset.sym = sym;
  row.innerHTML = `
    <span class="asset-sym">${sym.replace(/=X|\.T|=F/, '')}</span>
    <span class="asset-name">${meta.name || data.name || ''}</span>
    <span class="asset-price ${price ? '' : 'text-dim'}">${price ? fmtPrice(price, meta.currency) : '—'}</span>
    <span class="asset-chg ${chg > 0 ? 'up' : chg < 0 ? 'down' : ''}">${chg != null ? (chg > 0 ? '+' : '') + chg.toFixed(2) + '%' : ''}</span>
  `;
  row.addEventListener('click', () => selectSymbol(sym));
  return row;
}

function updateWatchlistRow(data, prev) {
  const row = document.querySelector(`[data-sym="${data.symbol}"]`);
  if (!row) { renderWatchlist(); return; }

  const priceEl = row.querySelector('.asset-price');
  const chgEl = row.querySelector('.asset-chg');
  const meta = catalogue[data.symbol] || {};
  const chg = data.change_pct ?? data.change_24h;

  if (priceEl) priceEl.textContent = fmtPrice(data.price, meta.currency);
  if (chgEl) {
    chgEl.textContent = chg != null ? (chg > 0 ? '+' : '') + chg.toFixed(2) + '%' : '';
    chgEl.className = 'asset-chg ' + (chg > 0 ? 'up' : chg < 0 ? 'down' : '');
  }

  // Flash animation
  if (prev && prev.price) {
    const cls = data.price > prev.price ? 'flash-up' : data.price < prev.price ? 'flash-down' : '';
    if (cls && priceEl) {
      priceEl.classList.remove('flash-up', 'flash-down');
      void priceEl.offsetWidth;
      priceEl.classList.add(cls);
      setTimeout(() => priceEl.classList.remove(cls), 400);
    }
  }
}

// ── Render: Quote bar ──────────────────────────────────────────────────────────
function updateQuoteBar(sym, prev = null) {
  const data = state.prices[sym] || {};
  const meta = catalogue[sym] || {};
  const price = data.price;
  const chg = data.change_pct ?? data.change_24h;

  document.getElementById('quote-symbol').textContent = sym;

  const priceEl = document.getElementById('quote-price');
  const oldPrice = parseFloat(priceEl.dataset.price || 0);
  priceEl.textContent = price ? fmtPrice(price, meta.currency) : '—';
  priceEl.dataset.price = price || 0;

  if (price && oldPrice) {
    const cls = price > oldPrice ? 'flash-up' : price < oldPrice ? 'flash-down' : '';
    if (cls) {
      priceEl.classList.remove('flash-up', 'flash-down');
      void priceEl.offsetWidth;
      priceEl.classList.add(cls);
      setTimeout(() => priceEl.classList.remove(cls), 500);
    }
  }

  const chgEl = document.getElementById('quote-change');
  if (chgEl) {
    chgEl.textContent = chg != null ? (chg > 0 ? '▲ +' : '▼ ') + chg.toFixed(2) + '%' : '';
    chgEl.className = 'quote-change ' + (chg > 0 ? 'up' : chg < 0 ? 'down' : '');
  }

  // Bid/Ask spread for crypto
  const bidEl = document.getElementById('quote-bid');
  const askEl = document.getElementById('quote-ask');
  if (bidEl && data.bid) bidEl.textContent = fmtPrice(data.bid, meta.currency);
  if (askEl && data.ask) askEl.textContent = fmtPrice(data.ask, meta.currency);

  const volEl = document.getElementById('quote-vol');
  if (volEl && data.volume_24h) {
    volEl.textContent = fmtVolume(data.volume_24h);
  }
}

// ── Render: Ticker tape ────────────────────────────────────────────────────────
const TAPE_SYMBOLS = ['BTC-USD', 'ETH-USD', 'USDJPY=X', 'ES=F', 'GC=F', '^N225', 'SOL-USD', 'EURUSD=X', 'NQ=F'];
function updateTickerTape(data) {
  if (!TAPE_SYMBOLS.includes(data.symbol)) return;
  const id = `tape-${data.symbol.replace(/[^a-zA-Z0-9]/g, '_')}`;
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('span');
    el.id = id;
    el.className = 'ticker-item';
    document.getElementById('ticker-inner').appendChild(el);
  }
  const chg = data.change_pct ?? data.change_24h ?? 0;
  const sym = data.symbol.replace(/=X|\.T|=F/, '');
  el.innerHTML = `
    <span class="ticker-sym">${sym}</span>
    <span class="ticker-price">${fmtPrice(data.price)}</span>
    <span class="ticker-chg ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>
  `;
}

// ── Render: Main chart (Lightweight Charts candlestick) ───────────────────────
function renderMainChart() {
  const container = document.getElementById('main-chart');
  if (!container) return;

  const candles = state.candles;
  if (!candles.length) return;

  // Destroy previous chart
  if (state.charts.main) {
    state.charts.main.remove();
    state.charts.main = null;
    container.innerHTML = '';
  }

  const chart = LightweightCharts.createChart(container, {
    width:  container.clientWidth,
    height: container.clientHeight || 320,
    layout: {
      background: { color: '#0d1410' },
      textColor:  '#4a6a4a',
      fontFamily: 'Courier New',
      fontSize:   11,
    },
    grid: {
      vertLines: { color: '#0d1a0d' },
      horzLines: { color: '#0d1a0d' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: '#4a6a4a', labelBackgroundColor: '#1a2e1a' },
      horzLine: { color: '#4a6a4a', labelBackgroundColor: '#1a2e1a' },
    },
    rightPriceScale: {
      borderColor: '#1a2e1a',
      textColor:   '#4a6a4a',
    },
    timeScale: {
      borderColor:     '#1a2e1a',
      timeVisible:     true,
      secondsVisible:  false,
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor:          '#00ff41',
    downColor:        '#ff3333',
    borderUpColor:    '#00ff41',
    borderDownColor:  '#ff3333',
    wickUpColor:      '#00cc33',
    wickDownColor:    '#cc2200',
  });

  // Lightweight Charts needs time in seconds (Unix), sorted ascending
  const ohlcData = candles
    .map(c => ({
      time:  Math.floor(c.time / 1000),
      open:  c.open,
      high:  c.high,
      low:   c.low,
      close: c.close,
    }))
    .sort((a, b) => a.time - b.time);

  candleSeries.setData(ohlcData);

  // Volume histogram
  const volSeries = chart.addHistogramSeries({
    color:       'rgba(0,255,65,0.2)',
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
  });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  const volData = candles
    .map(c => ({
      time:  Math.floor(c.time / 1000),
      value: c.volume || 0,
      color: c.close >= c.open ? 'rgba(0,255,65,0.25)' : 'rgba(255,51,51,0.25)',
    }))
    .sort((a, b) => a.time - b.time);

  volSeries.setData(volData);

  // Set initial visible range based on interval
  const visibleBars = { '1m': 120, '5m': 288, '15m': 192, '1h': 168, '1d': 30 };
  const bars = visibleBars[state.chartInterval] || 288;
  const lastTime = ohlcData[ohlcData.length - 1]?.time;
  const intervalSec = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400 };
  const sec = intervalSec[state.chartInterval] || 300;
  if (lastTime) {
    chart.timeScale().setVisibleRange({
      from: lastTime - bars * sec,
      to:   lastTime + sec * 2,
    });
  } else {
    chart.timeScale().fitContent();
  }

  // Resize observer
  const ro = new ResizeObserver(() => {
    chart.applyOptions({
      width:  container.clientWidth,
      height: container.clientHeight,
    });
  });
  ro.observe(container);

  state.charts.main = chart;
  state.charts._mainRo = ro;
}

// ── Render: Spread chart ───────────────────────────────────────────────────────
function renderSpreadChart() {
  const canvas = document.getElementById('spread-chart');
  if (!canvas) return;

  const history = state.spreadHistory.filter(s => s.spread_pct != null);
  if (history.length < 2) return;

  if (state.charts.spread) {
    state.charts.spread.destroy();
  }

  const labels = history.map(s => new Date(s.ts * 1000));
  const spreads = history.map(s => s.spread_pct);

  const lineColors = history.map(s => {
    if (s.signal === 'long')  return '#00ff41';
    if (s.signal === 'short') return '#ff3333';
    return '#4a6a4a';
  });

  state.charts.spread = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Spread %',
        data: spreads,
        borderColor: '#00cc33',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.2,
        fill: {
          target: 'origin',
          above: 'rgba(255,51,51,0.06)',
          below: 'rgba(0,255,65,0.06)',
        },
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0d1410',
          titleColor: '#00ff41',
          bodyColor: '#c0e0c0',
          callbacks: {
            title: items => new Date(items[0].parsed.x).toLocaleTimeString(),
            label: item => `Spread: ${item.parsed.y.toFixed(4)}%`,
          }
        }
      },
      scales: {
        x: {
          type: 'time',
          ticks: { display: false },
          grid:  { color: '#0d1a0d' },
        },
        y: {
          position: 'right',
          ticks: { color: '#4a6a4a', font: { family: 'Courier New', size: 9 },
            callback: v => v.toFixed(3) + '%' },
          grid: { color: '#0d1a0d' },
        }
      }
    }
  });
}

// ── Render: Spread stats ───────────────────────────────────────────────────────
function renderSpreadStats(snap) {
  const signal = snap.signal || 'neutral';
  const badge = document.getElementById('signal-badge');
  if (badge) {
    badge.className = `signal-badge signal-${signal}`;
    const icon = signal === 'long' ? '▲' : signal === 'short' ? '▼' : '●';
    badge.textContent = `${icon} ${signal.toUpperCase()} ${(snap.confidence * 100).toFixed(0)}%`;
  }

  const els = {
    'stat-coinbase':    snap.coinbase_price   ? fmtPrice(snap.coinbase_price) : '—',
    'stat-implied':     snap.polymarket_implied ? fmtPrice(snap.polymarket_implied) : 'N/A',
    'stat-spread-usd':  snap.spread_usd  != null ? '$' + snap.spread_usd.toFixed(2) : '—',
    'stat-spread-pct':  snap.spread_pct  != null ? snap.spread_pct.toFixed(4) + '%' : '—',
  };
  for (const [id, val] of Object.entries(els)) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }
}

// ── Render: Polymarket ────────────────────────────────────────────────────────
function renderPolymarkets() {
  const panel = document.getElementById('pm-list');
  if (!panel) return;
  panel.innerHTML = '';

  if (!state.polymarkets.length) {
    panel.innerHTML = '<div class="pm-market" style="color:#4a6a4a;font-size:10px;">No active BTC markets</div>';
    return;
  }

  for (const m of state.polymarkets.slice(0, 15)) {
    const yes = (m.yes_prob * 100).toFixed(1);
    const no  = (100 - m.yes_prob * 100).toFixed(1);
    const vol = m.volume > 1000 ? (m.volume / 1000).toFixed(1) + 'k' : m.volume.toFixed(0);

    const div = document.createElement('div');
    div.className = 'pm-market';
    div.innerHTML = `
      <div class="pm-question" title="${m.question}">${m.question}</div>
      <div class="pm-row">
        <span class="pm-yes">YES ${yes}%</span>
        <span class="pm-no">NO ${no}%</span>
        <span class="pm-volume">Vol: $${vol}</span>
      </div>
      <div class="prob-bar"><div class="prob-fill" style="width:${yes}%"></div></div>
    `;
    panel.appendChild(div);
  }
}

// ── Trading ────────────────────────────────────────────────────────────────────
function handleLabReport(msg) {
  const best = msg.best_strategy || '—';
  const jpy  = msg.best_daily_jpy || 0;
  const sign = jpy >= 0 ? '+' : '';
  log('signal', `[LAB] バックテスト完了 — 最優秀: ${best} ${sign}${Math.round(jpy).toLocaleString()}円/日`);
  (msg.strategies || []).forEach(s => {
    if (s.status === 'done') {
      const j = Math.round(s.daily_pnl_jpy || 0);
      log('info', `  ${s.name}: ${j >= 0 ? '+' : ''}${j.toLocaleString()}円/日 勝率${(s.win_rate||0).toFixed(1)}%`);
    }
  });
}

function handleTradeUpdate(msg) {
  if (msg.account) state.trading.account = msg.account;
  refreshTrading();
}

async function placeOrder(side) {
  const sym = state.selectedSymbol;
  const qty = parseFloat(document.getElementById('trade-qty').value);
  if (!qty || qty <= 0) return;

  const btnBuy  = document.getElementById('btn-buy');
  const btnSell = document.getElementById('btn-sell');
  btnBuy.disabled = btnSell.disabled = true;

  try {
    const res = await fetch(`${API}/trading/order`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ symbol: sym, side, qty }),
    });
    const order = await res.json();
    if (order.status === 'filled') {
      log('signal', `FILLED ${side.toUpperCase()} ${order.qty} ${sym} @ $${fmtPrice(order.fill_price)}`);
    } else {
      log('warn', `REJECTED: ${order.note || 'unknown reason'}`);
    }
    await refreshTrading();
  } catch (e) {
    log('error', `Order error: ${e.message}`);
  } finally {
    btnBuy.disabled = btnSell.disabled = false;
  }
}

async function refreshTrading() {
  try {
    const [accRes, posRes, ordRes] = await Promise.all([
      fetch(`${API}/trading/account`),
      fetch(`${API}/trading/positions`),
      fetch(`${API}/trading/orders?limit=20`),
    ]);
    state.trading.account   = await accRes.json();
    state.trading.positions = (await posRes.json()).positions;
    state.trading.orders    = (await ordRes.json()).orders;
    renderTradingPanel();
  } catch (e) {}
}

function renderTradingPanel() {
  const { cash, equity, starting_cash } = state.trading.account;
  const totalUpnl = (state.trading.positions || [])
    .reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const totalPnl = equity - starting_cash;

  document.getElementById('trade-cash').textContent   = '$' + fmt(cash);
  document.getElementById('trade-equity').textContent = '$' + fmt(equity);

  const upnlEl = document.getElementById('trade-upnl');
  upnlEl.textContent  = (totalUpnl >= 0 ? '+$' : '-$') + fmt(Math.abs(totalUpnl));
  upnlEl.className    = 'trade-value ' + (totalUpnl >= 0 ? 'up' : 'down');

  // Positions
  const posPanel = document.getElementById('positions-list');
  posPanel.innerHTML = '';
  if (!state.trading.positions?.length) {
    posPanel.innerHTML = '<div class="position-row ord-dim">No open positions</div>';
  }
  for (const p of (state.trading.positions || [])) {
    const pnl = p.unrealized_pnl || 0;
    const div = document.createElement('div');
    div.className = 'position-row';
    div.innerHTML =
      `<span class="pos-sym">${p.symbol.replace(/-USD$/, '')}</span>` +
      `<span class="pos-qty">${p.qty.toFixed(4)}</span>` +
      `<span class="ord-dim">avg $${fmtPrice(p.avg_price)}</span>` +
      `<span class="pos-pnl ${pnl >= 0 ? 'up' : 'down'}">${pnl >= 0 ? '+' : ''}$${fmt(pnl)}</span>`;
    posPanel.appendChild(div);
  }

  // Orders
  const ordPanel = document.getElementById('orders-list');
  ordPanel.innerHTML = '';
  for (const o of (state.trading.orders || []).slice(0, 8)) {
    const div = document.createElement('div');
    div.className = 'order-row';
    div.innerHTML =
      `<span class="ord-${o.side}">${o.side.toUpperCase()}</span>` +
      `<span class="ord-dim">${o.symbol.replace(/-USD$/, '')}</span>` +
      `<span>${(o.qty || 0).toFixed(4)}</span>` +
      `<span class="ord-dim">$${fmtPrice(o.fill_price)}</span>` +
      `<span class="${o.status === 'filled' ? 'up' : 'down'}">${o.status}</span>`;
    ordPanel.appendChild(div);
  }
}

// ── Select symbol ──────────────────────────────────────────────────────────────
function selectSymbol(sym) {
  state.selectedSymbol = sym;
  // Update active state in watchlist
  document.querySelectorAll('.asset-row').forEach(r => {
    r.classList.toggle('active', r.dataset.sym === sym);
  });
  updateQuoteBar(sym);
  loadOhlcv(sym);
  log('info', `Selected: ${sym}`);
}

// ── Chart interval selector ────────────────────────────────────────────────────
function setChartInterval(interval) {
  state.chartInterval = interval;
  document.querySelectorAll('.ctrl-btn[data-interval]').forEach(b => {
    b.classList.toggle('active', b.dataset.interval === interval);
  });
  loadOhlcv(state.selectedSymbol);
}

// ── Log ────────────────────────────────────────────────────────────────────────
function log(level, msg) {
  const ts = new Date().toLocaleTimeString('ja-JP', { hour12: false });
  state.logs.unshift({ ts, level, msg });
  if (state.logs.length > MAX_LOG) state.logs.pop();

  const panel = document.getElementById('log-list');
  if (!panel) return;

  const entry = document.createElement('div');
  entry.className = `log-entry log-${level}`;
  entry.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${msg}</span>`;
  panel.insertBefore(entry, panel.firstChild);

  while (panel.children.length > MAX_LOG) {
    panel.removeChild(panel.lastChild);
  }
}

// ── Status ─────────────────────────────────────────────────────────────────────
function setConnStatus(s) {
  const el = document.getElementById('conn-status');
  if (!el) return;
  el.textContent = { connected: '● LIVE', disconnected: '● OFFLINE', connecting: '○ CONNECTING' }[s] || s;
  el.className = `conn-status ${s}`;
}

// ── Formatters ──────────────────────────────────────────────────────────────────
function fmtPrice(price, currency = 'USD') {
  if (price == null || isNaN(price)) return '—';
  if (currency === 'JPY' || price > 500) {
    return price.toLocaleString('ja-JP', { maximumFractionDigits: 2 });
  }
  return price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

function fmtVolume(v) {
  if (!v) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K';
  return v.toFixed(0);
}

function fmt(n, d = 2) {
  if (n == null) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: d });
}

// ── Footer clock ───────────────────────────────────────────────────────────────
function startClock() {
  const el = document.getElementById('footer-clock');
  if (!el) return;
  const tick = () => {
    el.textContent = new Date().toLocaleString('ja-JP', {
      timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
    }) + ' JST';
  };
  tick();
  setInterval(tick, 1000);
}

// ── Init ────────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  startClock();

  // Trading buttons
  document.getElementById('btn-buy') .addEventListener('click', () => placeOrder('buy'));
  document.getElementById('btn-sell').addEventListener('click', () => placeOrder('sell'));

  // Initial data load
  await loadCatalogue();
  connectWS();
  await refreshTrading();

  // Periodic refreshes
  setInterval(loadSpreadHistory, 30_000);
  setInterval(loadPolymarkets,   60_000);
  setInterval(refreshTrading,    15_000);
  setInterval(() => {
    const el = document.getElementById('client-count');
    if (el) el.textContent = '1';  // self
  }, 5000);

  log('info', 'Terminal initialized');
  log('info', `Server: ${location.host}`);
});
