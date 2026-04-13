"""インジケーターブースト分析 — 既存手法のトレードに追加指標を重ねて勝率・利幅の相関を調べる.

使い方:
    results = await analyze_boost("MacdRci", "2432.T", params)
    → 各追加インジケーターの条件別に勝率・平均利益を集計
    → 「VWAP上 × RSI>50 の時は勝率78%, 平均利益+1,200円」のようなデータ

エントリー時点での各インジケーターの状態を記録し、
勝ちトレード/負けトレードで統計的に有意な差があるかを検出する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy
from backend.lab.runner import JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE

logger = logging.getLogger(__name__)


@dataclass
class BoostCondition:
    name: str           # "VWAP_above", "RSI_above_50", etc.
    total: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win_pnl: float = 0.0
    avg_loss_pnl: float = 0.0


@dataclass
class IndicatorConfig:
    """インジケーター設定。レジーム×時間足で最適値が変わる。"""
    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    ema_span: int = 20
    vol_avg_period: int = 20

    def to_dict(self) -> dict:
        return {
            "rsi": self.rsi_period, "bb": f"{self.bb_period}/{self.bb_std}σ",
            "atr": self.atr_period, "ema": self.ema_span, "vol": self.vol_avg_period,
        }


# レジーム別の推奨設定（デフォルトから探索で改善していく）
REGIME_INDICATOR_CONFIGS: dict[str, IndicatorConfig] = {
    "default":        IndicatorConfig(),
    "high_vol":       IndicatorConfig(rsi_period=10, bb_std=2.5, atr_period=10),
    "low_vol":        IndicatorConfig(rsi_period=20, bb_std=1.5, atr_period=20),
    "trending_up":    IndicatorConfig(rsi_period=10, bb_std=2.0, ema_span=15),
    "trending_down":  IndicatorConfig(rsi_period=10, bb_std=2.0, ema_span=15),
    "ranging":        IndicatorConfig(rsi_period=14, bb_std=2.0, bb_period=30),
}

# 探索用: パラメータ候補
INDICATOR_PARAM_VARIANTS = [
    IndicatorConfig(rsi_period=7,  bb_period=15, bb_std=1.5),
    IndicatorConfig(rsi_period=10, bb_period=20, bb_std=2.0),
    IndicatorConfig(rsi_period=14, bb_period=20, bb_std=2.0),  # デフォルト
    IndicatorConfig(rsi_period=14, bb_period=20, bb_std=2.5),
    IndicatorConfig(rsi_period=14, bb_period=30, bb_std=3.0),
    IndicatorConfig(rsi_period=20, bb_period=20, bb_std=1.5),
    IndicatorConfig(rsi_period=20, bb_period=30, bb_std=2.0),
]


def compute_indicators(df: pd.DataFrame,
                       config: IndicatorConfig | None = None) -> pd.DataFrame:
    """OHLCVに追加インジケーターを計算する。設定はconfigで変更可能。"""
    cfg = config or IndicatorConfig()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # VWAP（セッション内累積）
    typical = (high + low + close) / 3
    cum_vol = volume.cumsum()
    cum_tp_vol = (typical * volume).cumsum()
    df["vwap"] = cum_tp_vol / cum_vol.replace(0, np.nan)

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(cfg.rsi_period).mean()
    loss_val = (-delta.where(delta < 0, 0)).rolling(cfg.rsi_period).mean()
    rs = gain / loss_val.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ボリンジャーバンド
    sma = close.rolling(cfg.bb_period).mean()
    std = close.rolling(cfg.bb_period).std()
    df["bb_upper"] = sma + cfg.bb_std * std
    df["bb_lower"] = sma - cfg.bb_std * std
    df["bb_pct"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    # 出来高比率
    vol_avg = volume.rolling(cfg.vol_avg_period).mean()
    df["vol_ratio"] = volume / vol_avg.replace(0, np.nan)

    # ATR
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(cfg.atr_period).mean()
    df["atr_pct"] = df["atr"] / close * 100

    # EMA乖離
    ema = close.ewm(span=cfg.ema_span, adjust=False).mean()
    df["ema20_dev"] = (close - ema) / ema * 100

    return df


def classify_entry(row: pd.Series) -> dict[str, bool]:
    """エントリー時点のインジケーター状態を分類する。"""
    conditions = {}

    # VWAP
    if pd.notna(row.get("vwap")):
        conditions["VWAP上"] = row["close"] > row["vwap"]
        conditions["VWAP下"] = row["close"] < row["vwap"]

    # RSI
    if pd.notna(row.get("rsi")):
        conditions["RSI>60"] = row["rsi"] > 60
        conditions["RSI<40"] = row["rsi"] < 40
        conditions["RSI50-60"] = 50 <= row["rsi"] <= 60

    # ボリンジャーバンド位置
    if pd.notna(row.get("bb_pct")):
        conditions["BB上半分"] = row["bb_pct"] > 0.5
        conditions["BB下半分"] = row["bb_pct"] < 0.5
        conditions["BB上バンド近"] = row["bb_pct"] > 0.8
        conditions["BB下バンド近"] = row["bb_pct"] < 0.2

    # 出来高
    if pd.notna(row.get("vol_ratio")):
        conditions["出来高1.5倍超"] = row["vol_ratio"] > 1.5
        conditions["出来高0.5倍未満"] = row["vol_ratio"] < 0.5

    # ATR
    if pd.notna(row.get("atr_pct")):
        conditions["高ボラ(ATR>0.3%)"] = row["atr_pct"] > 0.3
        conditions["低ボラ(ATR<0.15%)"] = row["atr_pct"] < 0.15

    # EMA乖離
    if pd.notna(row.get("ema20_dev")):
        conditions["EMA上乖離>0.3%"] = row["ema20_dev"] > 0.3
        conditions["EMA下乖離<-0.3%"] = row["ema20_dev"] < -0.3

    return conditions


def analyze_trades_with_indicators(
    strategy_name: str,
    symbol: str,
    params: dict,
    df: pd.DataFrame,
    regime: str = "",
    config: IndicatorConfig | None = None,
) -> tuple[list[BoostCondition], dict]:
    """既存手法のトレードに追加指標を重ねて分析する。

    Returns:
        (conditions, meta)
        conditions: 各条件の統計
        meta: 使用したインジケーター設定とレジーム情報
    """
    # レジーム別の推奨設定を使用（指定があればそちら優先）
    if config is None:
        config = REGIME_INDICATOR_CONFIGS.get(regime,
                 REGIME_INDICATOR_CONFIGS["default"])

    meta = {
        "regime": regime,
        "indicator_config": config.to_dict(),
        "strategy": strategy_name,
        "symbol": symbol,
    }

    # インジケーター計算
    df = compute_indicators(df.copy(), config=config)

    # バックテスト実行
    strat = create_strategy(strategy_name, symbol, params=params)
    result = run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 25),
    )

    if not result.trades:
        return []

    # 各トレードのエントリー/エグジット時点のインジケーター状態を記録
    entry_stats: dict[str, BoostCondition] = {}
    exit_stats: dict[str, BoostCondition] = {}

    for trade in result.trades:
        pnl = trade.pnl
        is_win = pnl > 0

        # エントリー時点
        try:
            idx = df.index.get_indexer([pd.Timestamp(trade.entry_time)], method="nearest")[0]
            if 0 <= idx < len(df):
                entry_conds = classify_entry(df.iloc[idx])
                for cond_name, cond_met in entry_conds.items():
                    if not cond_met:
                        continue
                    key = f"entry:{cond_name}"
                    if key not in entry_stats:
                        entry_stats[key] = BoostCondition(name=key)
                    _record(entry_stats[key], pnl, is_win)
        except Exception:
            pass

        # エグジット時点（利伸ばし/早逃げ分析用）
        try:
            idx_exit = df.index.get_indexer([pd.Timestamp(trade.exit_time)], method="nearest")[0]
            if 0 <= idx_exit < len(df):
                exit_row = df.iloc[idx_exit]
                exit_conds = _classify_exit(exit_row, trade)
                for cond_name, cond_met in exit_conds.items():
                    if not cond_met:
                        continue
                    key = f"exit:{cond_name}"
                    if key not in exit_stats:
                        exit_stats[key] = BoostCondition(name=key)
                    _record(exit_stats[key], pnl, is_win)

                # エグジット前の中間状態（保有中のピーク/ボトム分析）
                if 0 <= idx < idx_exit:
                    holding_df = df.iloc[idx:idx_exit+1]
                    mid_conds = _classify_holding(holding_df, trade)
                    for cond_name, cond_met in mid_conds.items():
                        if not cond_met:
                            continue
                        key = f"holding:{cond_name}"
                        if key not in exit_stats:
                            exit_stats[key] = BoostCondition(name=key)
                        _record(exit_stats[key], pnl, is_win)
        except Exception:
            pass

    # 集計
    all_stats = {**entry_stats, **exit_stats}
    results = []
    for s in all_stats.values():
        if s.total < 5:
            continue
        s.win_rate = s.wins / s.total if s.total > 0 else 0
        s.avg_pnl = s.total_pnl / s.total if s.total > 0 else 0
        if s.wins > 0:
            s.avg_win_pnl = s.avg_win_pnl / s.wins
        if s.losses > 0:
            s.avg_loss_pnl = s.avg_loss_pnl / s.losses
        results.append(s)

    results.sort(key=lambda x: -x.win_rate)
    return results, meta


def analyze_best_config(
    strategy_name: str,
    symbol: str,
    params: dict,
    df: pd.DataFrame,
    regime: str = "",
) -> dict:
    """複数のインジケーター設定を試して最良の設定を見つける。"""
    best_config = None
    best_score = 0
    all_results = []

    for cfg in INDICATOR_PARAM_VARIANTS:
        try:
            conditions, meta = analyze_trades_with_indicators(
                strategy_name, symbol, params, df.copy(),
                regime=regime, config=cfg,
            )
            # 勝率70%以上の条件数 × 平均PnLでスコアリング
            high_wr = [c for c in conditions if c.win_rate >= 0.65 and c.total >= 5]
            score = sum(c.avg_pnl * c.win_rate for c in high_wr) if high_wr else 0
            all_results.append({
                "config": cfg.to_dict(),
                "high_wr_conditions": len(high_wr),
                "score": round(score, 0),
                "top_condition": high_wr[0].name if high_wr else None,
                "top_wr": round(high_wr[0].win_rate * 100) if high_wr else 0,
            })
            if score > best_score:
                best_score = score
                best_config = cfg
        except Exception:
            continue

    return {
        "best_config": best_config.to_dict() if best_config else None,
        "best_score": best_score,
        "regime": regime,
        "all_results": sorted(all_results, key=lambda x: -x["score"]),
    }


def _record(s: BoostCondition, pnl: float, is_win: bool) -> None:
    s.total += 1
    s.total_pnl += pnl
    if is_win:
        s.wins += 1
        s.avg_win_pnl += pnl
    else:
        s.losses += 1
        s.avg_loss_pnl += pnl


def _classify_exit(row: pd.Series, trade) -> dict[str, bool]:
    """エグジット時点の状態を分類する。"""
    conditions = {}
    if pd.notna(row.get("rsi")):
        conditions["RSI>70で利確"] = row["rsi"] > 70 and trade.pnl > 0
        conditions["RSI<30で損切"] = row["rsi"] < 30 and trade.pnl < 0
        conditions["RSIまだ60台(利伸ばし余地)"] = 55 <= row["rsi"] <= 65 and trade.pnl > 0
    if pd.notna(row.get("vwap")):
        conditions["VWAP割れで損切"] = row["close"] < row["vwap"] and trade.pnl < 0
        conditions["VWAP上で利確"] = row["close"] > row["vwap"] and trade.pnl > 0
    if pd.notna(row.get("vol_ratio")):
        conditions["出来高急増で決済"] = row["vol_ratio"] > 2.0
    if pd.notna(row.get("bb_pct")):
        conditions["BBバンド到達で利確"] = row["bb_pct"] > 0.9 and trade.pnl > 0
    return conditions


def _classify_holding(holding_df: pd.DataFrame, trade) -> dict[str, bool]:
    """保有中の状態を分析する（利を伸ばせたか、早逃げすべきだったか）。"""
    if holding_df.empty:
        return {}
    conditions = {}
    entry_price = trade.entry_price
    highs = holding_df["close"].values

    # 保有中の最大含み益 vs 実際のPnL
    if trade.side == "long" if hasattr(trade, 'side') else True:
        max_profit = (max(highs) - entry_price) * (trade.qty if hasattr(trade, 'qty') else 100)
        actual_pnl = trade.pnl
        # 最大含み益の半分以上を失って決済（早逃げすべきだった）
        if max_profit > 0 and actual_pnl < max_profit * 0.5:
            conditions["含み益50%以上失った"] = True
        # 最大含み益が大きいのに少ししか取れなかった（利伸ばし余地）
        if max_profit > 0 and actual_pnl > 0 and actual_pnl < max_profit * 0.3:
            conditions["含み益の30%未満で利確"] = True

    # VWAP割れが保有中に発生したか
    if "vwap" in holding_df.columns:
        vwap_cross = (holding_df["close"] < holding_df["vwap"]).any()
        if vwap_cross and trade.pnl < 0:
            conditions["保有中VWAP割れ→損失"] = True

    return conditions
