"""相場レジーム別バックテスト — 上昇・下落・レンジ・凪・急騰急落の5局面で検証.

フロー:
  1. 過去2年の日足データからレジーム（相場の種類）を分類
  2. 各レジームの代表期間を抽出
  3. その期間の5m足でバックテストを実行
  4. 定量スコア（勝率・PF・日次損益・DD）を集計
  5. 定性評価（「この手法はレンジ相場に強い」等）を自動生成

出力例:
  regime_report = {
    "uptrend":   {"daily_pnl": +4200, "win_rate": 62.3, "verdict": "強い"},
    "downtrend": {"daily_pnl": +1800, "win_rate": 55.1, "verdict": "普通"},
    "sideways":  {"daily_pnl":  -300, "win_rate": 44.2, "verdict": "弱い"},
    "volatile":  {"daily_pnl": +6100, "win_rate": 58.7, "verdict": "最強"},
    "calm":      {"daily_pnl":  +200, "win_rate": 50.0, "verdict": "やや弱い"},
  }
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backend.backtesting.engine import run_backtest, BacktestResult
from backend.strategies.base import StrategyBase

logger = logging.getLogger(__name__)

# ── レジーム定義 ───────────────────────────────────────────────────────────
REGIMES = {
    "uptrend":   "上昇相場",
    "downtrend":  "下落相場",
    "sideways":   "レンジ相場",
    "volatile":   "急騰急落",
    "calm":       "凪（低ボラ）",
}

VERDICT_THRESHOLDS = [
    (5000, "最強 ◎"),
    (2500, "強い ○"),
    (1000, "普通 △"),
    (0,    "やや弱い ▽"),
    (-999, "弱い ✕"),
]


@dataclass
class RegimePeriod:
    """1つのレジーム期間。"""
    regime:      str
    start_date:  str
    end_date:    str
    trend_20d:   float    # 20日リターン%
    volatility:  float    # ATR/価格
    rsi:         float


@dataclass
class RegimeResult:
    """1レジームのバックテスト集計結果。"""
    regime:       str
    regime_jp:    str
    periods_used: int
    daily_pnl_jpy: float
    win_rate:     float
    profit_factor: float
    max_dd_pct:   float
    sharpe:       float
    num_trades:   float   # 平均取引数
    verdict:      str     # 定性評価
    periods:      list[dict] = field(default_factory=list)


@dataclass
class RegimeBacktestReport:
    """全レジームのバックテストレポート。"""
    symbol:     str
    strategy_id: str
    results:    dict[str, RegimeResult]    # regime → RegimeResult
    best_regime:  str
    worst_regime: str
    summary:    str    # 人間が読める総評


def _classify_regime(row: pd.Series) -> str:
    """日足の特性からレジームを分類する。"""
    trend  = row.get("trend_20d", 0)
    vol    = row.get("atr_pct", 0)
    rsi    = row.get("rsi", 50)

    if vol < 0.008:
        return "calm"
    if vol > 0.025:
        return "volatile"
    if trend > 5:
        return "uptrend"
    if trend < -5:
        return "downtrend"
    return "sideways"


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _add_regime_features(df_daily: pd.DataFrame) -> pd.DataFrame:
    """日足DFにレジーム分類に必要な特徴量を追加する。"""
    d = df_daily.copy()
    d["trend_5d"]  = d["close"].pct_change(5)  * 100
    d["trend_20d"] = d["close"].pct_change(20) * 100
    d["atr_pct"]   = ((d["high"] - d["low"]) / d["close"]).rolling(10).mean()
    d["rsi"]       = _compute_rsi(d["close"])
    d["volume_ratio"] = d["volume"] / d["volume"].rolling(20).mean()
    d["regime"]    = d.apply(_classify_regime, axis=1)
    return d.dropna()


def _extract_regime_periods(
    df_feat: pd.DataFrame,
    regime:  str,
    window_days: int = 15,
    min_gap:     int = 20,
    max_periods: int = 5,
) -> list[RegimePeriod]:
    """指定レジームの連続期間をスライディングウィンドウで抽出する。"""
    periods: list[RegimePeriod] = []
    last_end_idx = -min_gap

    # 指定レジームが多数を占める窓を探す
    for i in range(window_days, len(df_feat)):
        window = df_feat.iloc[i - window_days: i]
        regime_ratio = (window["regime"] == regime).mean()
        if regime_ratio < 0.55:
            continue
        if i - last_end_idx < min_gap:
            continue

        start_date = str(df_feat.index[i - window_days])[:10]
        end_date   = str(df_feat.index[i - 1])[:10]
        row = window.iloc[-1]

        periods.append(RegimePeriod(
            regime     = regime,
            start_date = start_date,
            end_date   = end_date,
            trend_20d  = round(float(row.get("trend_20d", 0)), 2),
            volatility = round(float(row.get("atr_pct", 0)), 4),
            rsi        = round(float(row.get("rsi", 50)), 1),
        ))
        last_end_idx = i

        if len(periods) >= max_periods:
            break

    return periods


def _verdict(daily_pnl: float) -> str:
    for threshold, label in VERDICT_THRESHOLDS:
        if daily_pnl >= threshold:
            return label
    return "弱い ✕"


def _stat_regime_eval(
    df_feat: pd.DataFrame,
    bt_kwargs: dict,
) -> dict[str, "RegimeResult"]:
    """5mデータなしで日足統計から相場別スコアを推定する。

    スカルプ戦略の特性:
    - 高ボラ(ATR>2%) + トレンド → 動きが大きく利幅を取りやすい
    - 低ボラ(ATR<1.2%) = 凪 → スプレッドコスト割れリスク
    - 急騰急落(ATR>2.5%) → TP到達しやすいが損切りも多い
    - レンジ → 上下するのでEMAクロス効果あり/なし混在

    推定日次PnL = position_value × atr_pct × signal_quality - cost
    signal_quality ∈ [0.3, 0.8] (レジームと一致性によって調整)
    """
    # T1レベルの典型的ポジションサイズ（3,000円株 × 100株 = 300,000円）
    typical_position = 300_000.0

    # EMA 3/5 スカルプのデフォルトTP/SL
    tp_pct = 0.005
    sl_pct = 0.003

    # 相場レジーム別の推定パラメータ（バックテスト実績から推定）
    REGIME_PRIORS: dict[str, dict] = {
        "calm":      {"win_rate": 44, "trades_day": 1.0, "adj": -1.0},  # 凪→スカルプ不利
        "sideways":  {"win_rate": 50, "trades_day": 2.5, "adj":  0.0},  # レンジ→方向感なし
        "uptrend":   {"win_rate": 58, "trades_day": 3.0, "adj": +1.0},  # 上昇→ロング有利
        "downtrend": {"win_rate": 55, "trades_day": 2.5, "adj": +0.5},  # 下落→ショート有利
        "volatile":  {"win_rate": 53, "trades_day": 4.0, "adj": +0.5},  # 急騰急落→TP到達しやすい
    }

    results: dict[str, RegimeResult] = {}

    for regime, regime_jp in REGIMES.items():
        periods = _extract_regime_periods(df_feat, regime, window_days=20, max_periods=5)
        if not periods:
            continue

        priors = REGIME_PRIORS.get(regime, {"win_rate": 50, "trades_day": 2, "adj": 0})

        period_data = []
        for p in periods:
            mask = (df_feat.index.date >= pd.Timestamp(p.start_date).date()) & \
                   (df_feat.index.date <= pd.Timestamp(p.end_date).date())
            seg = df_feat[mask]
            if seg.empty:
                continue

            atr    = float(seg["atr_pct"].mean())
            vol_r  = float(seg["volume_ratio"].mean())

            # ボラティリティで勝率を微調整
            if atr < 0.01:
                atr_adj = -5   # 凪 → 不利
            elif atr > 0.03:
                atr_adj = -3   # 過剰ボラ → 損切りリスク増
            elif 0.015 < atr < 0.025:
                atr_adj = +3   # 最適帯
            else:
                atr_adj = 0

            # 出来高で取引機会を調整
            trades_adj = 1.0 + min(vol_r - 1.0, 0.5)   # 出来高増 → 機会増

            win_rate = float(priors["win_rate"]) + atr_adj
            win_rate = max(35.0, min(68.0, win_rate))   # 35-68%にクランプ

            trades_day = priors["trades_day"] * trades_adj

            wr_frac = win_rate / 100
            pnl_per_trade = typical_position * (wr_frac * tp_pct - (1 - wr_frac) * sl_pct)
            daily_pnl = pnl_per_trade * trades_day

            period_data.append({
                "start": p.start_date,
                "end":   p.end_date,
                "trend_20d": p.trend_20d,
                "volatility": p.volatility,
                "atr_pct": round(atr, 4),
                "est_win_rate": round(win_rate, 1),
                "est_daily_pnl": round(daily_pnl, 0),
            })

        if not period_data:
            continue

        avg_pnl = float(np.mean([d["est_daily_pnl"] for d in period_data]))
        avg_wr  = float(np.mean([d["est_win_rate"]  for d in period_data]))

        results[regime] = RegimeResult(
            regime        = regime,
            regime_jp     = regime_jp,
            periods_used  = len(period_data),
            daily_pnl_jpy = round(avg_pnl, 0),
            win_rate      = round(avg_wr, 2),
            profit_factor = round(avg_wr / max(1, 100 - avg_wr), 3),
            max_dd_pct    = 0.0,   # 統計推定では算出不可
            sharpe        = 0.0,
            num_trades    = 0.0,
            verdict       = _verdict(avg_pnl),
            periods       = period_data,
        )

    return results


class RegimeBacktester:
    """相場レジーム別バックテストを実行する。"""

    def __init__(self, bt_kwargs: dict[str, Any] | None = None):
        self.bt_kwargs = bt_kwargs or {}

    async def run(
        self,
        strategy:       StrategyBase,
        symbol:         str,
        lookback_days:  int = 504,     # 約2年分の日足
        window_days:    int = 15,      # 1期間の長さ
    ) -> RegimeBacktestReport:
        """全レジームでバックテストを実行してレポートを返す。"""

        # 日足データ取得
        df_daily = await self._fetch_daily(symbol, lookback_days)
        if df_daily is None or df_daily.empty:
            raise ValueError(f"日足データ取得失敗: {symbol}")

        df_feat = _add_regime_features(df_daily)
        logger.info("レジーム分類完了 %s: %s",
                    symbol, df_feat["regime"].value_counts().to_dict())

        # 5m足データ取得（最大60日）
        df_5m = await self._fetch_5m(symbol)

        # 5m取得失敗時は日足プロキシモードで統計評価
        use_daily_proxy = (df_5m is None or df_5m.empty)
        if use_daily_proxy:
            logger.info("5mデータ取得失敗 → 日足統計評価モード: %s", symbol)
            # 日足統計評価: 実際のBTなしで相場特性からスコアを推定
            regime_results = _stat_regime_eval(df_feat, self.bt_kwargs)
            best  = max(regime_results, key=lambda k: regime_results[k].daily_pnl_jpy) \
                    if regime_results else "不明"
            worst = min(regime_results, key=lambda k: regime_results[k].daily_pnl_jpy) \
                    if regime_results else "不明"
            summary = self._generate_summary(
                strategy.meta.symbol, regime_results, best, worst,
                proxy_mode=True
            )
            return RegimeBacktestReport(
                symbol       = symbol,
                strategy_id  = strategy.meta.id,
                results      = regime_results,
                best_regime  = best,
                worst_regime = worst,
                summary      = summary,
            )

        regime_results: dict[str, RegimeResult] = {}

        for regime, regime_jp in REGIMES.items():
            periods = _extract_regime_periods(
                df_feat, regime, window_days=window_days
            )
            if not periods:
                logger.debug("期間なし: %s / %s", symbol, regime)
                continue

            bt_results: list[BacktestResult] = []
            used_periods = []

            for p in periods:
                df_slice = self._slice_5m(df_5m, p.start_date, p.end_date)
                if df_slice is None or len(df_slice) < 30:
                    continue

                try:
                    r = run_backtest(strategy, df_slice, **self.bt_kwargs)
                    if r.num_trades >= 1:
                        bt_results.append(r)
                        used_periods.append({
                            "start": p.start_date,
                            "end":   p.end_date,
                            "trend_20d": p.trend_20d,
                            "volatility": p.volatility,
                            "num_trades": r.num_trades,
                            "daily_pnl":  round(r.daily_pnl_jpy, 0),
                            "win_rate":   round(r.win_rate, 1),
                        })
                except Exception as e:
                    logger.debug("BT失敗 %s %s: %s", regime, p.start_date, e)

            if not bt_results:
                continue

            # 集計（平均）
            avg_daily  = np.mean([r.daily_pnl_jpy   for r in bt_results])
            avg_wr     = np.mean([r.win_rate         for r in bt_results])
            avg_pf     = np.mean([r.profit_factor    for r in bt_results])
            avg_dd     = np.mean([r.max_drawdown_pct for r in bt_results])
            avg_sharpe = np.mean([r.sharpe           for r in bt_results])
            avg_trades = np.mean([r.num_trades       for r in bt_results])

            regime_results[regime] = RegimeResult(
                regime        = regime,
                regime_jp     = regime_jp,
                periods_used  = len(bt_results),
                daily_pnl_jpy = round(float(avg_daily), 0),
                win_rate      = round(float(avg_wr), 2),
                profit_factor = round(float(avg_pf), 3),
                max_dd_pct    = round(float(avg_dd), 2),
                sharpe        = round(float(avg_sharpe), 3),
                num_trades    = round(float(avg_trades), 1),
                verdict       = _verdict(float(avg_daily)),
                periods       = used_periods,
            )

        # 総評生成
        best  = max(regime_results, key=lambda k: regime_results[k].daily_pnl_jpy) \
                if regime_results else "不明"
        worst = min(regime_results, key=lambda k: regime_results[k].daily_pnl_jpy) \
                if regime_results else "不明"

        summary = self._generate_summary(
            strategy.meta.symbol, regime_results, best, worst,
            proxy_mode=use_daily_proxy
        )

        return RegimeBacktestReport(
            symbol      = symbol,
            strategy_id = strategy.meta.id,
            results     = regime_results,
            best_regime  = best,
            worst_regime = worst,
            summary     = summary,
        )

    def _slice_daily(
        self, df_daily: pd.DataFrame,
        start: str, end: str
    ) -> pd.DataFrame | None:
        """日足データを期間でスライスし、タイムスタンプを前場セッション内(10:00 JST)に揃える。
        JPScalpの時間フィルター（9:10-11:30）を通過させるため。
        """
        try:
            mask = (df_daily.index.date >= pd.Timestamp(start).date()) & \
                   (df_daily.index.date <= pd.Timestamp(end).date())
            sliced = df_daily[mask][["open", "high", "low", "close", "volume"]].copy()
            if sliced.empty:
                return None
            # タイムスタンプを各日の 10:00 JST に変換（前場セッション内に入れる）
            from datetime import datetime
            new_idx = pd.to_datetime([
                datetime(ts.year, ts.month, ts.day, 10, 0, 0)
                for ts in sliced.index
            ]).tz_localize("Asia/Tokyo", ambiguous="infer", nonexistent="shift_forward")
            sliced.index = new_idx
            return sliced
        except Exception as e:
            logger.debug("_slice_daily error: %s", e)
            return None

    def _slice_5m(
        self, df_5m: pd.DataFrame | None,
        start: str, end: str
    ) -> pd.DataFrame | None:
        if df_5m is None or df_5m.empty:
            return None
        try:
            mask = (df_5m.index.date >= pd.Timestamp(start).date()) & \
                   (df_5m.index.date <= pd.Timestamp(end).date())
            sliced = df_5m[mask]
            return sliced if not sliced.empty else None
        except Exception:
            return None

    @staticmethod
    async def _fetch_daily(symbol: str, days: int) -> pd.DataFrame | None:
        """日足データを取得する。J-Quants → yfinance の順で試みる。"""
        # J-Quants APIを優先（JP株のみ・より安定）
        if symbol.endswith(".T"):
            try:
                from backend.feeds.jquants_client import get_daily_quotes_df, is_available
                if is_available():
                    df = await asyncio.wait_for(
                        get_daily_quotes_df(symbol, days=max(days, 504)),
                        timeout=30,
                    )
                    if df is not None and not df.empty:
                        # adj列優先、なければ生値を使う
                        col_map: dict[str, str] = {}
                        if "adj_close" in df.columns:
                            col_map = {
                                "adj_open":   "open",
                                "adj_high":   "high",
                                "adj_low":    "low",
                                "adj_close":  "close",
                                "adj_volume": "volume",
                            }
                            # adj列を新カラム名で取り出す
                            needed_adj = [c for c in col_map if c in df.columns]
                            out = df[needed_adj].rename(columns=col_map)
                        else:
                            needed = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
                            out = df[needed]

                        if {"open", "high", "low", "close", "volume"}.issubset(out.columns):
                            idx = pd.to_datetime(out.index)
                            if idx.tz is None:
                                idx = idx.tz_localize("Asia/Tokyo", ambiguous="infer", nonexistent="shift_forward")
                            else:
                                idx = idx.tz_convert("Asia/Tokyo")
                            out.index = idx
                            return out[["open", "high", "low", "close", "volume"]].tail(days)
            except Exception as e:
                logger.debug("J-Quants daily fetch failed %s: %s", symbol, e)

        # yfinance フォールバック
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _yf_daily(symbol, days)),
                timeout=25,
            )
        except Exception:
            return None

    @staticmethod
    async def _fetch_5m(symbol: str) -> pd.DataFrame | None:
        loop = asyncio.get_event_loop()
        for period in ("60d", "30d", "14d"):
            try:
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda p=period: _yf_5m(symbol, p)
                    ),
                    timeout=25,
                )
                if result is not None and not result.empty:
                    return result
            except Exception:
                continue
            await asyncio.sleep(1)
        return None

    @staticmethod
    def _generate_summary(
        symbol: str,
        results: dict[str, RegimeResult],
        best: str,
        worst: str,
        proxy_mode: bool = False,
    ) -> str:
        if not results:
            return "データ不足のため評価不可"

        best_jp  = results[best].regime_jp  if best  in results else best
        worst_jp = results[worst].regime_jp if worst in results else worst
        best_pnl  = results[best].daily_pnl_jpy  if best  in results else 0
        worst_pnl = results[worst].daily_pnl_jpy if worst in results else 0

        mode = "※日足プロキシ評価" if proxy_mode else "5分足評価"
        lines = [
            f"【{symbol} 相場レジーム別評価】({mode})",
            f"最強レジーム: {best_jp}（平均 {best_pnl:+,.0f}円/日）",
            f"最弱レジーム: {worst_jp}（平均 {worst_pnl:+,.0f}円/日）",
        ]

        # プラスのレジームを列挙
        positive = [r for r in results.values() if r.daily_pnl_jpy > 0]
        negative = [r for r in results.values() if r.daily_pnl_jpy <= 0]
        if positive:
            lines.append(f"利益が出る相場: " +
                         "、".join(r.regime_jp for r in positive))
        if negative:
            lines.append(f"注意が必要な相場: " +
                         "、".join(r.regime_jp for r in negative))

        # 運用アドバイス
        if worst in results and results[worst].daily_pnl_jpy < -500:
            lines.append(f"→ {worst_jp}では取引を控えるか損失上限を厳しく設定推奨")
        if best in results and results[best].daily_pnl_jpy > 3000:
            lines.append(f"→ {best_jp}では積極的にポジションを増やす余地あり")

        return "\n".join(lines)


def _yf_daily(symbol: str, days: int) -> pd.DataFrame | None:
    import yfinance as yf
    import warnings; warnings.filterwarnings("ignore")
    months = min(days // 30 + 1, 24)
    df = yf.Ticker(symbol).history(period=f"{months}mo", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo")
    return df[["open", "high", "low", "close", "volume"]].tail(days)


def _yf_5m(symbol: str, period: str = "60d") -> pd.DataFrame | None:
    import yfinance as yf
    import warnings; warnings.filterwarnings("ignore")
    df = yf.Ticker(symbol).history(period=period, interval="5m", auto_adjust=True)
    if df is None or df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo")
    return df[["open", "high", "low", "close", "volume"]]
