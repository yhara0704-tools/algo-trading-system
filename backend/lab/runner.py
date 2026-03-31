"""Lab Runner — 並列バックテスト + PDCA自動管理.

起動時・6時間ごとに自動実行:
  1. JP株スクリーニング（上位5銘柄選定）
  2. BTC戦略 × 全パラメータ セット バックテスト
  3. JP選定銘柄 × ORB/VWAP バックテスト
  4. PDCA評価 → 次アクション自動生成
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import pandas as pd

from backend.backtesting.engine import BacktestResult, run_backtest
from backend.capital_tier import get_tier, TIERS, CapitalTier
from backend.strategies.base import StrategyBase
from backend.strategies.btc.ema_cross import BTCEmaCross
from backend.strategies.btc.rsi_bb import BTCRsiBollinger
from backend.strategies.btc.vwap_reversion import BTCVwapReversion
from backend.strategies.jp_stock.screener import screen_stocks, ScreenResult
from backend.strategies.jp_stock.pts_screener import pts_screen, PTSResult
from backend.strategies.jp_stock.jp_orb import JPOpeningRangeBreakout
from backend.strategies.jp_stock.jp_vwap import JPVwapReversion
from backend.strategies.jp_stock.jp_momentum_5min import JPMomentum5Min
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.market_regime import MarketRegimeDetector
from backend.notify import push
from backend.analysis.time_pattern import get_store as get_pattern_store
from backend.analysis.strategy_knowledge import get_kb, analyze_failure
from backend.analysis.overfitting_guard import OverfittingGuard
from backend.analysis.regime_matcher import get_matcher
from backend.analysis.regime_backtest import RegimeBacktester, RegimeBacktestReport

logger = logging.getLogger(__name__)

_BINANCE_REST = "https://api.binance.com/api/v3"
_SYMBOL_MAP   = {"BTC-USD": "BTCUSDT"}
_INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}

# ── 資金設定 ───────────────────────────────────────────────────────────────────
# 楽天証券ゼロコース: 信用取引手数料0円・日計り信用金利0円
# 信用取引最低委託保証金: 30万円（法定）
# 委託保証金率30% → 30万円 × 3.33倍 = 約99万円建玉可能
INITIAL_CAPITAL_JPY: float = 300_000.0      # 総資金 30万円（松井証券 一日信用 最低ライン）
BTC_CAPITAL_JPY:     float = 100_000.0      # BTC用  10万円
JP_CAPITAL_JPY:      float = 300_000.0      # JP株用 30万円（松井一日信用・手数料/金利完全0円）
USD_JPY_RATE:        float = 150.0
POSITION_PCT:        float = 0.5            # 1トレードに資金の50%まで

# 信用倍率（松井証券 一日信用）: 委託保証金率30% → 最大3.3倍
MARGIN_RATIO: float = 3.3
# JP株の有効建玉上限: JP_CAPITAL_JPY × MARGIN_RATIO
JP_MAX_POSITION_JPY: float = JP_CAPITAL_JPY * MARGIN_RATIO   # 99万円

# 1単元(100株)が買える最大株価
# JP_MAX_POSITION_JPY × POSITION_PCT / 100株
LOT_SIZE: int = 100   # JP株の最低売買単位（100株）
MAX_STOCK_PRICE: float = JP_MAX_POSITION_JPY * POSITION_PCT / LOT_SIZE  # 4,950円/株

BTC_CAPITAL_USD: float = BTC_CAPITAL_JPY / USD_JPY_RATE   # ≈ 667 USD

# ── 資金マイルストーン（元本複利・追加入金なし） ──────────────────────────────
# 日次0.5%複利を前提とした参考値
# 30万 → 50万: 約102取引日(5ヶ月)  30万 → 100万: 約242日(1.2年)
@dataclass
class CapitalMilestone:
    name:          str
    target_jpy:    float   # 目標総資産（円）
    daily_pnl_jpy: float   # そのステージでの日次目標収益
    description:   str

CAPITAL_MILESTONES = [
    CapitalMilestone("M1", 500_000,   2_500, "50万円達成 — 初期検証完了"),
    CapitalMilestone("M2", 1_000_000, 5_000, "100万円達成 — 安定稼働"),
    CapitalMilestone("M3", 3_000_000, 15_000, "300万円達成 — 本格運用開始"),
    CapitalMilestone("M4", 10_000_000, 50_000, "1,000万円達成 — プロ水準"),
]

# ── PDCA 目標ステージ ──────────────────────────────────────────────────────────
@dataclass
class PDCAGoal:
    stage:         int
    daily_pnl_jpy: float
    win_rate:      float
    max_drawdown:  float
    description:   str

PDCA_STAGES = [
    PDCAGoal(1,  1_000, 52, -8,  "毎日1,000円 — まず負けない（元本30万円・信用口座）"),
    PDCAGoal(2,  2_500, 55, -6,  "毎日2,500円 — 50万円達成後"),
    PDCAGoal(3,  5_000, 57, -5,  "毎日5,000円 — 100万円達成後"),
    PDCAGoal(4,  15_000, 60, -4, "毎日15,000円 — 300万円達成後"),
]

@dataclass
class PDCAStatus:
    current_stage:  int    = 1
    best_result_id: str    = ""
    best_daily_jpy: float  = 0.0
    goal_met:       bool   = False
    next_action:    str    = "初回バックテスト待機中..."
    last_updated:   str    = ""
    run_count:      int    = 0
    screen_results: list   = field(default_factory=list)


# ── データ取得 ─────────────────────────────────────────────────────────────────
async def _fetch_binance_ohlcv(symbol: str, interval: str, days: int) -> pd.DataFrame:
    binance_sym = _SYMBOL_MAP.get(symbol, symbol.replace("-", "").upper())
    bi          = _INTERVAL_MAP.get(interval, "5m")
    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24}.get(bi, 288)
    needed = days * bars_per_day
    limit  = 1000
    pages  = min((needed + limit - 1) // limit, 10)

    all_rows: list = []
    end_time = None

    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(pages):
            params: dict[str, Any] = {"symbol": binance_sym, "interval": bi, "limit": limit}
            if end_time:
                params["endTime"] = end_time
            try:
                resp = await client.get(f"{_BINANCE_REST}/klines", params=params)
                resp.raise_for_status()
                rows = resp.json()
                if not rows:
                    break
                all_rows = rows + all_rows
                end_time = rows[0][0] - 1
            except Exception:
                break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","num_trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time").sort_index()
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df.index = df.index.tz_convert("Asia/Tokyo")
    return df[["open","high","low","close","volume"]]


async def _fetch_yfinance_ohlcv(symbol: str, interval: str, days: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    try:
        df = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _yf_fetch(symbol, interval, days)),
            timeout=25,
        )
        return df if df is not None else pd.DataFrame()
    except asyncio.TimeoutError:
        logger.warning("yfinance timeout %s", symbol)
        return pd.DataFrame()


def _yf_fetch(symbol: str, interval: str, days: int) -> pd.DataFrame | None:
    import yfinance as yf
    period = f"{min(days, 59)}d"  # yfinance 1m/5m limit: 60 days
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = pd.to_datetime(df.index, utc=True).tz_localize("UTC")
    df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo")
    return df[["open","high","low","close","volume"]]


async def fetch_ohlcv(symbol: str, interval: str, days: int) -> pd.DataFrame:
    if symbol in _SYMBOL_MAP or symbol.endswith("-USD"):
        return await _fetch_binance_ohlcv(symbol, interval, days)
    return await _fetch_yfinance_ohlcv(symbol, interval, days)


# ── 戦略ファクトリ ─────────────────────────────────────────────────────────────
def get_btc_strategies() -> list[StrategyBase]:
    """1m / 5m / 15m の3時間軸で全戦略を検証する。
    スキャルピングは1m、短期デイは5m、デイトレは15mが本命。
    """
    strategies = []
    for tf in ("1m", "5m", "15m"):
        strategies.append(BTCEmaCross(interval=tf))
        strategies.append(BTCRsiBollinger(interval=tf))
        strategies.append(BTCVwapReversion(interval=tf))
    return strategies


def get_jp_strategies(screen_results: list[ScreenResult],
                       use_time_patterns: bool = True) -> list[StrategyBase]:
    """スクリーニング結果から日本株戦略を生成。
    1340通り総当たり検証で発見した最適パラメータを銘柄別に設定。
    use_time_patterns=True の場合、蓄積済みの時間帯パターンをフィルターに反映する。
    """
    strategies: list[StrategyBase] = []
    selected = [r for r in screen_results if r.selected]
    pattern_store = get_pattern_store() if use_time_patterns else None

    # 銘柄別最適パラメータ（1340通り総当たり検証結果）
    # Scalp1m: EMA(fast,slow) / TP / SL / ATR_min / allow_short
    # allow_short: 空売り側がプラスの銘柄のみTrue（貸株料年1.1%込み試算）
    _BEST_SCALP1M: dict[str, dict] = {
        "2413.T":  dict(ema_fast=3, ema_slow=8,  tp_pct=0.004, sl_pct=0.002, atr_min_pct=0.001, allow_short=True),   # M3     短売+2400円/日
        "3697.T":  dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),   # SHIFT  短売+4486円/日
        "7267.T":  dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),   # Honda  短売+364円/日
        "6645.T":  dict(ema_fast=3, ema_slow=8,  tp_pct=0.003, sl_pct=0.002, atr_min_pct=0.001, allow_short=True),   # Omron  短売+314円/日
        "4568.T":  dict(ema_fast=5, ema_slow=10, tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),   # DaiichiSankyo 短売+643円/日
        "9432.T":  dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=False),  # NTT    短売-229円/日 → OFF
        "7203.T":  dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),   # Toyota 短売+243円/日
        "9433.T":  dict(ema_fast=3, ema_slow=5,  tp_pct=0.004, sl_pct=0.002, atr_min_pct=0.001, allow_short=False),  # KDDI   短売-793円/日 → OFF
        "8306.T":  dict(ema_fast=5, ema_slow=13, tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),   # MUFG   短売+329円/日
    }
    # デフォルト（未登録銘柄用）
    _DEFAULT_SCALP1M = dict(ema_fast=3, ema_slow=8, tp_pct=0.004, sl_pct=0.002, atr_min_pct=0.001, allow_short=True)

    for r in selected:
        params = _BEST_SCALP1M.get(r.symbol, _DEFAULT_SCALP1M)

        # ── 主力: 最適Scalp1m（総当たり検証で選ばれたパラメータ） ──────────────
        scalp1m = JPScalp(r.symbol, r.name, morning_only=True, interval="1m", **params)
        strategies.append(scalp1m)

        # ── 補助: Scalp5m（1m データがない場合の代替・長期検証用） ───────────────
        scalp5m = JPScalp(r.symbol, r.name, interval="5m", morning_only=True,
                          ema_fast=params["ema_fast"], ema_slow=params["ema_slow"],
                          tp_pct=params["tp_pct"]*1.5, sl_pct=params["sl_pct"]*1.5,
                          atr_min_pct=0.002)
        scalp5m.meta.id   += "_5m"
        scalp5m.meta.name += " [5m]"
        strategies.append(scalp5m)

        # ── 既存戦略（ORB・VWAP・Breakout）も比較用に残す ─────────────────────
        strategies.append(JPOpeningRangeBreakout(r.symbol, r.name, range_minutes=10, tp_ratio=2.0, sl_ratio=1.0))
        strategies.append(JPBreakout(r.symbol, r.name, interval="5m"))

        # TriChemのみ Momentum5m が最良だったので追加
        if r.symbol == "4369.T":
            mom = JPMomentum5Min(r.symbol, r.name, mom_pct=0.005, tp_pct=0.010, sl_pct=0.006, vol_mult=1.2)
            strategies.append(mom)

        # 時間帯パターンが蓄積されていれば、それを利用したバリアントも追加
        if pattern_store and use_time_patterns:
            zones = pattern_store.get_danger_zones(r.symbol, min_samples=5)
            high_vol  = zones.get("high_vol_slots", [])
            no_trend  = zones.get("no_trend_slots", [])
            avoid     = list(set(high_vol + no_trend))
            if avoid:
                # ORB: 高ボラ寄り付きを避けるバリアント
                orb_v = JPOpeningRangeBreakout(
                    r.symbol, r.name,
                    avoid_opening_minutes=5,
                    avoid_slots=avoid,
                )
                orb_v.meta.id   += "_timefilter"
                orb_v.meta.name += " (時間フィルター)"
                strategies.append(orb_v)

                # VWAP: 高ボラ帯をスキップするバリアント
                vwap_v = JPVwapReversion(
                    r.symbol, r.name, avoid_slots=avoid,
                )
                vwap_v.meta.id   += "_timefilter"
                vwap_v.meta.name += " (時間フィルター)"
                strategies.append(vwap_v)

                # Momentum: 危険時間帯を回避するバリアント
                mom_v = JPMomentum5Min(r.symbol, r.name, avoid_slots=avoid)
                mom_v.meta.id   += "_timefilter"
                mom_v.meta.name += " (時間フィルター)"
                strategies.append(mom_v)

    return strategies


# ── Lab Runner ─────────────────────────────────────────────────────────────────
class LabRunner:
    def __init__(self) -> None:
        self._results:  dict[str, BacktestResult] = {}
        self._running:  set[str] = set()
        self._pdca      = PDCAStatus()
        self._executor  = ThreadPoolExecutor(max_workers=6)
        self._regime    = MarketRegimeDetector()
        self._notified_stages: set[int] = set()
        self._btc_cycle:  int = 0   # BTC専用サイクルカウンタ
        self._regime_analysis: dict[str, dict] = {}  # symbol → regime report dict

    def get_results(self) -> list[dict]:
        snapshot = list(self._results.values())  # 辞書変更と競合しないようコピー
        return sorted(
            [self._result_to_dict(r) for r in snapshot],
            key=lambda r: r.get("score", 0),
            reverse=True,
        )

    def get_result(self, strategy_id: str) -> dict | None:
        r = self._results.get(strategy_id)
        return self._result_to_dict(r) if r else None

    def get_running(self) -> list[str]:
        return list(self._running)

    def build_daily_summary(self, jp_session: dict | None = None) -> str:
        """1日の検証結果サマリー文字列を生成（Pushover送信用）。"""
        results = self.get_results()
        done    = [r for r in results if r.get("num_trades", 0) > 0]
        pdca    = self.get_pdca()
        regime  = self.get_regime()

        lines = [f"📈 今日の検証サマリー ({pdca['run_count']}回目)"]
        lines.append(f"地合い: {regime.get('BTC-USD', {}).get('regime_jp', '不明')}")
        lines.append("")

        if done:
            best = max(done, key=lambda r: r.get("score", 0))
            worst = min(done, key=lambda r: r.get("score", 0))
            positive = [r for r in done if r.get("daily_pnl_jpy", 0) > 0]

            lines.append(f"検証戦略数: {len(done)}")
            lines.append(f"プラス戦略: {len(positive)}/{len(done)}")
            lines.append(f"【最良】{best['strategy_name']}")
            lines.append(f"  日次 {best.get('daily_pnl_jpy',0):+,.0f}円 / 勝率 {best.get('win_rate',0):.1f}%")
            if worst != best:
                lines.append(f"【最悪】{worst['strategy_name']}")
                lines.append(f"  日次 {worst.get('daily_pnl_jpy',0):+,.0f}円 / 勝率 {worst.get('win_rate',0):.1f}%")
        else:
            lines.append("検証結果なし")

        lines.append("")
        lines.append(f"PDCAステージ: {pdca['current_stage']} — {pdca.get('goal',{}).get('description','')}")
        lines.append(f"次のアクション: {pdca.get('next_action','—')[:60]}")

        if jp_session and jp_session.get("num_trades", 0) > 0:
            lines.append("")
            lines.append(f"【JP株リアル】{jp_session['date']}")
            lines.append(f"  損益 {jp_session['total_pnl']:+,.0f}円 / {jp_session['num_trades']}件")
            lines.append(f"  サブセッション: {len(jp_session.get('subsessions',[]))}回")

        return "\n".join(lines)

    def get_regime(self) -> dict:
        return self._regime.get_all()

    def get_pdca(self) -> dict:
        stage = PDCA_STAGES[min(self._pdca.current_stage - 1, len(PDCA_STAGES) - 1)]
        return {
            "current_stage":  self._pdca.current_stage,
            "run_count":      self._pdca.run_count,
            "btc_cycle":      self._btc_cycle,
            "goal":           dataclasses.asdict(stage),
            "best_daily_jpy": self._pdca.best_daily_jpy,
            "best_result_id": self._pdca.best_result_id,
            "goal_met":       self._pdca.goal_met,
            "next_action":    self._pdca.next_action,
            "last_updated":   self._pdca.last_updated,
            "all_stages":     [dataclasses.asdict(s) for s in PDCA_STAGES],
            "screen_results": self._pdca.screen_results,
        }

    def get_live_jp_strategies(self) -> list[StrategyBase]:
        """最新のスクリーニング結果から JP 株戦略を返す（リアルタイム用）。"""
        cached = getattr(self, "_cached_screen", [])
        if not cached:
            return []
        use_tp = self._pdca.run_count > 1
        return get_jp_strategies(cached, use_time_patterns=use_tp)

    def get_knowledge(self) -> list[dict]:
        """全戦略の知識ベース（地合い別実績・洞察）を返す。"""
        return get_kb().get_all()

    def get_regime_map(self) -> dict:
        """地合い別の推奨戦略マップを返す。"""
        return get_kb().get_regime_map()

    def get_regime_analysis(self) -> dict:
        """全銘柄のレジーム別バックテスト結果を返す。"""
        return self._regime_analysis

    async def run_regime_analysis(
        self,
        symbol: str,
        strategy_id: str | None = None,
    ) -> dict:
        """指定銘柄のレジーム別バックテストを実行してレポートを返す。"""
        # strategy_idが指定されていれば既存結果から戦略を探す
        if strategy_id:
            all_strats = get_btc_strategies() + get_jp_strategies(
                getattr(self, "_cached_screen", []), use_time_patterns=False
            )
            strat_map = {s.meta.id: s for s in all_strats}
            if strategy_id in strat_map:
                strategy = strat_map[strategy_id]
            else:
                raise ValueError(f"Unknown strategy: {strategy_id}")
        else:
            # symbol から直接戦略を生成（最適パラメータを使用）
            from backend.strategies.jp_stock.jp_scalp import JPScalp
            _BEST = {
                "2413.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.004, sl_pct=0.002, allow_short=True),
                "3697.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "7267.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "6645.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.003, sl_pct=0.002, allow_short=True),
                "4568.T": dict(ema_fast=5, ema_slow=10, tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "9432.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, allow_short=False),
                "7203.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "9433.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.004, sl_pct=0.002, allow_short=False),
                "8306.T": dict(ema_fast=5, ema_slow=13, tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "6758.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, allow_short=True),
                "6098.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, allow_short=True),
            }
            params = _BEST.get(symbol, dict(ema_fast=3, ema_slow=8, tp_pct=0.004, sl_pct=0.002, allow_short=True))
            name = symbol.replace(".T", "")
            strategy = JPScalp(symbol, name, interval="5m", **params)

        tier = get_tier(JP_CAPITAL_JPY)
        bt_kwargs = dict(
            starting_cash=JP_CAPITAL_JPY * tier.margin,
            fee_pct=0.0,
            position_pct=tier.position_pct,
            usd_jpy=1.0,
            lot_size=LOT_SIZE,
            limit_slip_pct=0.003,
            short_borrow_fee_annual=0.011,
        )

        backtester = RegimeBacktester(bt_kwargs=bt_kwargs)
        report = await backtester.run(strategy, symbol)

        report_dict = {
            "symbol":      report.symbol,
            "strategy_id": report.strategy_id,
            "best_regime":  report.best_regime,
            "worst_regime": report.worst_regime,
            "summary":     report.summary,
            "results": {
                regime: {
                    "regime_jp":    r.regime_jp,
                    "periods_used": r.periods_used,
                    "daily_pnl_jpy": r.daily_pnl_jpy,
                    "win_rate":     r.win_rate,
                    "profit_factor": r.profit_factor,
                    "max_dd_pct":   r.max_dd_pct,
                    "sharpe":       r.sharpe,
                    "num_trades":   r.num_trades,
                    "verdict":      r.verdict,
                    "periods":      r.periods,
                }
                for regime, r in report.results.items()
            },
        }
        self._regime_analysis[symbol] = report_dict
        return report_dict

    async def run_btc_only(self, days: int = 14, usd_jpy: float = 150.0) -> list[dict]:
        """BTC戦略のみの高速サイクル（10分ごと）。JP株は含まない。"""
        self._btc_cycle += 1
        logger.info("BTC cycle #%d start (days=%d)", self._btc_cycle, days)

        # 地合い更新
        try:
            btc_df = await _fetch_binance_ohlcv("BTC-USD", "1h", 7)
            if not btc_df.empty:
                await self._regime.update("BTC-USD", btc_df)
        except Exception:
            pass

        btc_strats = get_btc_strategies() + self._generate_variants()
        tasks  = [self._run_one(s, days=days, usd_jpy=usd_jpy) for s in btc_strats]
        raw    = await asyncio.gather(*tasks, return_exceptions=True)
        done   = []
        for s, r in zip(btc_strats, raw):
            if isinstance(r, Exception):
                done.append({"status": "error", "strategy_id": s.meta.id, "error": str(r)})
            else:
                done.append(r)

        prev_stage = self._pdca.current_stage
        self._auto_pdca(done, usd_jpy)
        await self._notify_if_stage_cleared(prev_stage, done)

        # 知識ベース保存（10サイクルごと）
        if self._btc_cycle % 10 == 0:
            get_kb().save()

        return done

    async def run_all(self, days: int = 30, usd_jpy: float = 150.0) -> list[dict]:
        """スクリーニング → BTC + JP + パラメータ探索 を並列バックテスト。"""
        self._pdca.run_count += 1
        logger.info("Lab run #%d start", self._pdca.run_count)

        # 1. JP株スクリーニング（初回 or 6回に1回実行）+ PTS候補マージ
        if self._pdca.run_count == 1 or self._pdca.run_count % 6 == 0:
            logger.info("Screening JP stocks...")
            try:
                screen_results, pts_results = await asyncio.gather(
                    asyncio.wait_for(screen_stocks(top_n=5, capital_jpy=JP_CAPITAL_JPY, margin=MARGIN_RATIO), timeout=120),
                    asyncio.wait_for(pts_screen(top_n=3), timeout=120),
                    return_exceptions=True,
                )
                if isinstance(screen_results, Exception):
                    logger.warning("Screening failed: %s", screen_results)
                    screen_results = getattr(self, "_cached_screen", [])
                if isinstance(pts_results, Exception):
                    logger.warning("PTS screen failed: %s", pts_results)
                    pts_results = []

                # PTS候補を通常スクリーニング結果にマージ（重複除外）
                existing_syms = {r.symbol for r in screen_results}
                for pts_r in pts_results:
                    if pts_r.selected and pts_r.symbol not in existing_syms:
                        # PTSResult は ScreenResult と互換フィールドを持つ
                        screen_results.append(pts_r)  # type: ignore[arg-type]
                        existing_syms.add(pts_r.symbol)

                self._pdca.screen_results = [
                    dataclasses.asdict(r) if hasattr(r, '__dataclass_fields__') else vars(r)
                    for r in screen_results
                ]
                self._cached_screen = screen_results
                self._cached_pts    = [r for r in pts_results if not isinstance(r, Exception)]
                logger.info("Selected: %s (+ PTS: %s)",
                            [r.symbol for r in screen_results if r.selected and not getattr(r, 'is_pts_candidate', False)],
                            [r.symbol for r in screen_results if r.selected and getattr(r, 'is_pts_candidate', False)])
            except Exception as exc:
                logger.warning("Screening failed: %s", exc)
                screen_results = getattr(self, "_cached_screen", [])
        else:
            screen_results = getattr(self, "_cached_screen", [])

        # 2. 地合い判定（BTCのみ — JP株は場中でないと意味が薄い）
        try:
            btc_df = await _fetch_binance_ohlcv("BTC-USD", "1h", 30)
            if not btc_df.empty:
                await self._regime.update("BTC-USD", btc_df)
        except Exception as exc:
            logger.debug("Regime update failed: %s", exc)

        # 3. 基本戦略 + パラメータバリアントを生成
        btc_strats = get_btc_strategies() + self._generate_variants()
        # JP戦略: 2回目以降は時間帯パターンフィルターを有効化
        use_tp = self._pdca.run_count > 1
        jp_strats  = get_jp_strategies(screen_results, use_time_patterns=use_tp)
        all_strats = btc_strats + jp_strats

        tasks = [self._run_one(s, days=days, usd_jpy=usd_jpy) for s in all_strats]
        raw   = await asyncio.gather(*tasks, return_exceptions=True)

        done = []
        for s, r in zip(all_strats, raw):
            if isinstance(r, Exception):
                done.append({"status": "error", "strategy_id": s.meta.id, "error": str(r)})
            else:
                done.append(r)

        # 4. PDCA自動評価 + ステージ達成時Pushover通知
        prev_stage = self._pdca.current_stage
        self._auto_pdca(done, usd_jpy)
        await self._notify_if_stage_cleared(prev_stage, done)

        logger.info("Lab run #%d complete. Best=%.0fJPY/day Action: %s",
                    self._pdca.run_count, self._pdca.best_daily_jpy,
                    self._pdca.next_action[:60])
        return done

    async def _notify_if_stage_cleared(self, prev_stage: int, results: list[dict]) -> None:
        """ステージが上がったらPushoverで通知（重複通知防止）。"""
        new_stage = self._pdca.current_stage
        if new_stage <= prev_stage:
            return
        cleared = new_stage - 1
        if cleared in self._notified_stages:
            return
        self._notified_stages.add(cleared)

        goal = PDCA_STAGES[cleared - 1]
        valid = [r for r in results if r.get("status") == "done" and r.get("num_trades", 0) > 0]
        best  = max(valid, key=lambda r: r.get("score", 0)) if valid else {}

        title = f"🎉 Stage {cleared} 達成！"
        msg   = (
            f"{goal.description}\n"
            f"最優秀戦略: {best.get('strategy_name', '—')}\n"
            f"日次損益: {best.get('daily_pnl_jpy', 0):+,.0f}円/日\n"
            f"勝率: {best.get('win_rate', 0):.1f}%\n"
            f"→ 次の目標 Stage {new_stage}: "
            f"{PDCA_STAGES[min(new_stage-1, len(PDCA_STAGES)-1)].daily_pnl_jpy:,.0f}円/日"
        )
        await push(title, msg, priority=1)

    def _generate_variants(self) -> list[StrategyBase]:
        """上位戦略のパラメータをグリッドサーチ的に変化させてバリアントを生成。"""
        variants: list[StrategyBase] = []

        # 既存結果がなければスキップ
        if not self._results:
            return variants

        # スコア上位3戦略のパラメータを変動させる
        top = sorted(self._results.values(), key=lambda r: r.score, reverse=True)[:3]

        for base_result in top:
            sid = base_result.strategy_id
            # ベース戦略のタイムフレームを引き継ぐ
            tf = base_result.params.get("interval", "5m") if hasattr(base_result, "params") else "5m"
            # strategy_idからタイムフレームを取得
            for candidate_tf in ("1m", "5m", "15m"):
                if sid.endswith(f"_{candidate_tf}"):
                    tf = candidate_tf
                    break

            base_sid = sid.replace(f"_{tf}", "")  # tf部分を除いたベースID

            if "btc_ema_cross" in base_sid:
                for fast, slow in [(5,13),(7,21),(9,26),(12,26)]:
                    for tp in [0.004, 0.006, 0.010]:
                        vid = f"btc_ema_{fast}_{slow}_tp{int(tp*1000)}_{tf}"
                        if vid not in self._results or self._results[vid].score < base_result.score * 0.8:
                            s = BTCEmaCross(ema_fast=fast, ema_slow=slow,
                                            stop_pct=0.002, tp_pct=tp, interval=tf)
                            s.meta.id   = vid
                            s.meta.name = f"EMA{fast}/{slow} TP{int(tp*1000)} [{tf}]"
                            variants.append(s)
            elif "btc_rsi_bb" in base_sid:
                for rsi_e, bb_s in [(30,2.0),(35,1.8),(40,2.2)]:
                    for stop in [0.002, 0.004]:
                        vid = f"btc_rsibb_r{rsi_e}_b{int(bb_s*10)}_s{int(stop*1000)}_{tf}"
                        if vid not in self._results:
                            s = BTCRsiBollinger(rsi_entry=rsi_e, bb_std=bb_s,
                                                stop_pct=stop, interval=tf)
                            s.meta.id   = vid
                            s.meta.name = f"RSI{rsi_e} BB{bb_s} SL{int(stop*1000)} [{tf}]"
                            variants.append(s)
            elif "btc_vwap" in base_sid:
                for dev, stop in [(0.002,0.003),(0.004,0.005),(0.006,0.007)]:
                    vid = f"btc_vwap_d{int(dev*1000)}_s{int(stop*1000)}_{tf}"
                    if vid not in self._results:
                        s = BTCVwapReversion(dev_pct=dev, stop_pct=stop, interval=tf)
                        s.meta.id   = vid
                        s.meta.name = f"VWAP dev{int(dev*1000)} sl{int(stop*1000)} [{tf}]"
                        variants.append(s)

        # 1回のランで最大15バリアントに制限
        return variants[:15]

    async def run_backtest(self, strategy_id: str, days: int = 30,
                           usd_jpy: float = 150.0) -> dict:
        """単一戦略を手動実行（APIから呼ばれる場合）。"""
        all_strats = get_btc_strategies()
        strat_map  = {s.meta.id: s for s in all_strats}
        if strategy_id not in strat_map:
            raise ValueError(f"Unknown strategy: {strategy_id}")
        return await self._run_one(strat_map[strategy_id], days=days, usd_jpy=usd_jpy)

    async def _run_one(self, strategy: StrategyBase,
                       days: int, usd_jpy: float,
                       use_similar_period: bool = True) -> dict:
        sid = strategy.meta.id
        if sid in self._running:
            return {"status": "already_running", "strategy_id": sid}
        self._running.add(sid)
        try:
            df = await fetch_ohlcv(strategy.meta.symbol, strategy.meta.interval, days)
            if df.empty:
                raise ValueError("No data")

            # 時間帯パターン記録（JP株のみ）
            if strategy.meta.symbol.endswith(".T"):
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        self._executor,
                        lambda: get_pattern_store().record_from_df(strategy.meta.symbol, df)
                    )
                except Exception:
                    pass

            # JP株はJPY建て(usd_jpy=1.0)、BTCはUSD建て(usd_jpy=rate)
            is_jp = strategy.meta.symbol.endswith(".T")
            s_usd_jpy = 1.0 if is_jp else usd_jpy
            s_lot_size = LOT_SIZE if is_jp else 1

            if is_jp:
                # 現資金に応じたティアを取得してポジションサイズを決定
                tier = get_tier(JP_CAPITAL_JPY)
                s_cash     = JP_CAPITAL_JPY * tier.margin   # buying power
                s_pos_pct  = tier.position_pct
                # 銘柄の流動性上限をposition_pctに反映
                liq_limit  = tier.effective_position(strategy.meta.symbol)
                max_by_liq = liq_limit / s_cash if s_cash > 0 else s_pos_pct
                s_pos_pct  = min(s_pos_pct, max_by_liq)
            else:
                s_cash    = BTC_CAPITAL_USD
                s_pos_pct = POSITION_PCT

            # JP株: 松井一日信用は手数料0円
            # limit_slip_pct=0.003: 次足始値が指値から0.3%以上離れたら見送り
            s_fee  = 0.0 if is_jp else 0.001   # BTC(Binance)は0.1%taker
            s_slip = 0.003 if is_jp else 0.0   # JP株のみ指値スルー判定を適用
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                lambda: run_backtest(
                    strategy, df,
                    starting_cash=s_cash,
                    fee_pct=s_fee,
                    position_pct=s_pos_pct,
                    usd_jpy=s_usd_jpy,
                    lot_size=s_lot_size,
                    limit_slip_pct=s_slip,
                    short_borrow_fee_annual=0.011 if is_jp else 0.0,
                )
            )
            self._results[sid] = result
            result_dict = {"status": "done", **self._result_to_dict(result)}

            # ── 過学習チェック（JP株のみ・非同期で実行） ──────────────────
            if is_jp and result.num_trades >= 5:
                try:
                    bt_kw = dict(
                        starting_cash=s_cash, fee_pct=s_fee,
                        position_pct=s_pos_pct, usd_jpy=s_usd_jpy,
                        lot_size=s_lot_size, limit_slip_pct=s_slip,
                        short_borrow_fee_annual=0.011,
                    )
                    guard  = OverfittingGuard()
                    report = await loop.run_in_executor(
                        self._executor,
                        lambda: guard.evaluate(strategy, df, bt_kw)
                    )
                    result_dict["overfitting"] = {
                        "is_robust":    report.is_robust,
                        "warnings":     report.warnings,
                        "penalty":      report.score_penalty,
                        "oos_ratio":    report.wf.oos_ratio   if report.wf else None,
                        "stability":    report.robustness.stability if report.robustness else None,
                        "shuffle_p":    report.shuffle.p_value     if report.shuffle else None,
                    }
                    # スコアにペナルティを反映
                    result_dict["score"] = round(
                        result_dict.get("score", 0) + report.score_penalty, 2
                    )
                except Exception:
                    pass

            # ── 類似相場期間での追加検証（JP株のみ・3サイクルに1回） ──────
            if is_jp and use_similar_period and self._pdca.run_count % 3 == 0:
                try:
                    matcher = get_matcher()
                    periods = await matcher.find_similar_periods(
                        strategy.meta.symbol, lookback_days=504
                    )
                    if periods:
                        best_p = periods[0]
                        result_dict["similar_period"] = {
                            "start":       best_p.start_date,
                            "end":         best_p.end_date,
                            "similarity":  best_p.similarity,
                            "regime":      best_p.fingerprint.regime,
                            "description": best_p.description,
                        }
                        logger.info("類似相場 %s: %s〜%s (sim=%.3f)",
                                    strategy.meta.symbol,
                                    best_p.start_date, best_p.end_date,
                                    best_p.similarity)
                except Exception:
                    pass

            # 知識ベースに記録（地合いを付加して保存）
            try:
                regime_info = self._regime.get_all()
                regime_str  = regime_info.get("BTC-USD", {}).get("regime", "不明") \
                              if "BTC-USD" in str(strategy.meta.symbol) \
                              else "不明"
                goal = PDCA_STAGES[min(self._pdca.current_stage - 1, len(PDCA_STAGES)-1)]
                get_kb().record(result_dict, regime=regime_str,
                                target_daily_jpy=goal.daily_pnl_jpy)
            except Exception:
                pass

            return result_dict
        except Exception as exc:
            return {"status": "error", "strategy_id": sid, "error": str(exc)}
        finally:
            self._running.discard(sid)

    def _auto_pdca(self, results: list[dict], usd_jpy: float) -> None:
        """PDCA自動評価 — 最良戦略を特定し次アクションを自動生成。"""
        valid = [r for r in results
                 if r.get("status") == "done" and r.get("num_trades", 0) > 0]
        if not valid:
            self._pdca.next_action = "データ取得失敗。次回実行を待機中..."
            return

        # 最良戦略
        best = max(valid, key=lambda r: r.get("score", 0))
        if best["daily_pnl_jpy"] > self._pdca.best_daily_jpy:
            self._pdca.best_daily_jpy = best["daily_pnl_jpy"]
            self._pdca.best_result_id = best["strategy_id"]

        goal = PDCA_STAGES[min(self._pdca.current_stage - 1, len(PDCA_STAGES) - 1)]
        goal_met = (
            best["daily_pnl_jpy"]    >= goal.daily_pnl_jpy and
            best["win_rate"]         >= goal.win_rate and
            best["max_drawdown_pct"] >= goal.max_drawdown
        )

        if goal_met and not self._pdca.goal_met:
            self._pdca.goal_met = True
            if self._pdca.current_stage < len(PDCA_STAGES):
                self._pdca.current_stage += 1
                next_goal = PDCA_STAGES[self._pdca.current_stage - 1]
                self._pdca.next_action = (
                    f"✅ Stage {self._pdca.current_stage - 1} 達成！"
                    f" → Stage {self._pdca.current_stage}: {next_goal.description}"
                    f" (目標 {next_goal.daily_pnl_jpy:,.0f}円/日)"
                )
                self._pdca.goal_met = False  # reset for next stage
            else:
                self._pdca.next_action = "🏆 全ステージ達成！さらなる最適化へ"
        else:
            gap = goal.daily_pnl_jpy - best["daily_pnl_jpy"]
            tips = []
            if best["win_rate"] < goal.win_rate:
                tips.append(f"勝率{best['win_rate']:.1f}%→{goal.win_rate}%へ: エントリー条件を絞る")
            if best.get("profit_factor", 0) < 1.5:
                tips.append("PF低い: TP幅を拡大 or SL幅を縮小")
            if best["max_drawdown_pct"] < goal.max_drawdown:
                tips.append(f"DD{best['max_drawdown_pct']:.1f}%超: ポジションサイズ縮小")
            if not tips:
                tips.append(f"取引回数を増やす（現在{best['num_trades']}回/30日）")

            best_name = best.get("strategy_name", "?")
            self._pdca.next_action = (
                f"最優秀: {best_name} | あと{gap:,.0f}円/日 → " + " / ".join(tips)
            )

        self._pdca.last_updated = str(pd.Timestamp.now("Asia/Tokyo"))[:19]

    def _result_to_dict(self, r: BacktestResult) -> dict:
        d = dataclasses.asdict(r)
        eq = d.pop("equity_curve", [])
        d["equity_curve"] = eq[::max(1, len(eq)//200)]
        d["trades"] = d["trades"][-50:]
        return d
