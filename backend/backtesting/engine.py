"""Backtesting engine — event-driven, SL/TP aware.

Processes OHLCV candles one at a time to simulate realistic order fills:
- Entry on signal bar close (or next open for realism)
- Stop loss / take profit checked against candle high/low
- Fee applied on both entry and exit
- Position sizing: fixed fraction of equity

Returns BacktestResult with full trade log and equity curve.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase


def _attach_extra_ohlcv(strategy: StrategyBase, extra: dict[str, pd.DataFrame] | None) -> None:
    """MTF 戦略向け: ``attach(df_d=..., df_h1=...)`` に 1d/1h を渡す。"""
    if not extra:
        return
    attach = getattr(strategy, "attach", None)
    if not callable(attach):
        return
    kw: dict[str, pd.DataFrame | None] = {}
    dfd = extra.get("1d")
    if dfd is not None and not getattr(dfd, "empty", True):
        kw["df_d"] = dfd
    dfh = extra.get("1h")
    if dfh is not None and not getattr(dfh, "empty", True):
        kw["df_h1"] = dfh
    if kw:
        attach(**kw)


@dataclass
class Trade:
    entry_time:   str
    exit_time:    str
    symbol:       str
    side:         str        # "long"
    entry_price:  float
    exit_price:   float
    qty:          float
    pnl:          float      # in quote currency (USD or JPY)
    pnl_pct:      float
    exit_reason:  str        # "signal" | "stop_loss" | "take_profit" | "end"
    duration_bars: int


@dataclass
class BacktestResult:
    strategy_id:   str
    strategy_name: str
    symbol:        str
    interval:      str
    start_date:    str
    end_date:      str
    params:        dict

    trades:         list[Trade] = field(default_factory=list)
    equity_curve:   list[float] = field(default_factory=list)

    # Metrics (filled by compute_metrics)
    total_return_pct:  float = 0.0
    win_rate:          float = 0.0
    profit_factor:     float = 0.0
    max_drawdown_pct:  float = 0.0
    sharpe:            float = 0.0
    num_trades:        int   = 0
    avg_trade_pct:     float = 0.0
    avg_duration_bars: float = 0.0
    daily_pnl_jpy:     float = 0.0   # avg daily P&L in JPY
    daily_pnl_usd:     float = 0.0
    gross_profit_jpy:  float = 0.0   # 総利益（勝ちトレード合計）JPY/日
    gross_loss_jpy:    float = 0.0   # 総損失（負けトレード合計、負値）JPY/日
    avg_win_jpy:       float = 0.0   # 平均利益/トレード (JPY)
    avg_loss_jpy:      float = 0.0   # 平均損失/トレード (JPY, 負値)
    days_tested:       int   = 0     # バックテスト期間（日数）
    score:             float = 0.0   # composite score for PDCA ranking
    # 複利スケール指標（Phase A: 1億到達まで追跡）
    cagr:                  float = 0.0   # 年率複利成長率 (%)
    calmar:                float = 0.0   # CAGR / |MDD%|
    mar:                   float = 0.0   # = calmar (alias)
    daily_return_pct_mean: float = 0.0   # 日次リターンの平均 (%)
    daily_return_pct_std:  float = 0.0   # 日次リターンの標準偏差 (%)
    # サブセッション統計: [{day, slot_end, reason, pnl_pct}]
    subsession_stats:  list  = field(default_factory=list)


def run_backtest(
    strategy:              StrategyBase,
    df:                    pd.DataFrame,
    starting_cash:         float = 100_000.0,
    fee_pct:               float = 0.001,    # 0.1% per side (Binance taker)
    position_pct:          float = 0.5,      # use 50% of equity per trade
    usd_jpy:               float = 150.0,    # for JPY conversion
    daily_loss_limit_pct:  float = -1.0,     # サブセッション損失上限 (-1%) ※損失防止のみ
    daily_profit_pct:      float = 999.0,    # 利益上限は実質撤廃（良い日は稼ぎ切る）
    subsession_cooldown_min: int = 30,       # ルール発動後の再開待ち時間（分）
    lot_size:              int   = 1,        # 最低売買単位（JP株=100, BTC=1）
    limit_slip_pct:        float = 0.003,    # 指値スルー判定: 次足始値が指値からこれ以上離れたらスキップ
    short_borrow_fee_annual: float = 0.0,    # デイトレ信用 貸株料0%（通常銘柄）
    short_premium_daily_pct: float = 0.0,   # プレミアム料（前日終値×%/日、0=なし）
    long_margin_interest_annual: float = 0.0,  # Phase D4: 信用買い金利（年率）
    latency_bars: int = 0,  # Phase E1: シグナル発生から約定までの追加遅延バー数（0=既定1バー）
    volume_impact_coeff: float = 0.0,  # Phase E2: 出来高参加率スリッページ係数（0=無効、0.5程度が保守的）
    eod_close_time: tuple[int, int] | None = None,  # JP株1日信用の強制クローズ時刻 e.g. (14, 25)
    gate = None,  # AgentGate インスタンス（None = ゲートなし）
    extra_ohlcv: dict[str, pd.DataFrame] | None = None,
    # JPParabolicSwing 等: {"1d": df_daily, "1h": df_hourly} を渡すと attach される
) -> BacktestResult:
    """Run backtest. df must have DatetimeIndex and OHLCV columns.

    損益ルールはサブセッション制で運用する:
      - ルール発動 → ポジションクローズ → cooldown_min 待機 → 新サブセッション開始
      - 1日中繰り返すことで「時間帯×戦略」の良し悪しデータが蓄積される
      - 全日終了後にサブセッション統計を result.subsession_stats に格納

    指値スルー判定 (limit_slip_pct):
      - シグナル発生バーの終値で指値を置く想定
      - 次足の始値が指値から limit_slip_pct 以上乖離していたら約定しなかったとみなしスキップ
      - 0.0 で無効化（常に約定）
    """
    _attach_extra_ohlcv(strategy, extra_ohlcv)

    sig_df = strategy.generate_signals(df)

    # エージェントゲート（オプション）: run_backtest の gate 引数で渡す
    if gate is not None:
        sig_df = gate.apply(sig_df)

    result = BacktestResult(
        strategy_id=strategy.meta.id,
        strategy_name=strategy.meta.name,
        symbol=strategy.meta.symbol,
        interval=strategy.meta.interval,
        start_date=str(df.index[0])[:10],
        end_date=str(df.index[-1])[:10],
        params=strategy.meta.params.copy(),
    )

    equity    = starting_cash
    position  = None
    equity_curve = [equity]
    trades    = []

    # サブセッション管理
    current_day:           str   = ""
    subsession_start_eq:   float = starting_cash   # このサブセッション開始時の資産
    resume_after_ts:       pd.Timestamp | None = None   # None = active
    subsession_stats:      list  = []   # [{day, slot_start, slot_end, reason, pnl_pct}]

    cooldown = pd.Timedelta(minutes=subsession_cooldown_min)

    # Phase E1: シグナル参照を latency_bars ぶん過去に遡る。既定は 0 で従来通り (prev = i-1)。
    lat = max(0, int(latency_bars))
    for i in range(1 + lat, len(sig_df)):
        row   = sig_df.iloc[i]
        prev  = sig_df.iloc[i - 1 - lat]
        price = row["close"]
        ts    = str(sig_df.index[i])
        ts_dt = sig_df.index[i]

        # --- 日付変わりでサブセッションリセット ---
        bar_day = ts[:10]
        if bar_day != current_day:
            current_day        = bar_day
            subsession_start_eq = equity
            resume_after_ts    = None

        # --- クールダウン解除チェック ---
        if resume_after_ts is not None and ts_dt >= resume_after_ts:
            resume_after_ts    = None
            subsession_start_eq = equity   # 新サブセッション開始

        # --- 損益ルールチェック（ポジションなし＆クールダウン外のみ） ---
        if position is None and resume_after_ts is None:
            sub_pnl_pct = (equity - subsession_start_eq) / subsession_start_eq * 100
            trigger_reason: str | None = None
            if sub_pnl_pct <= daily_loss_limit_pct:
                trigger_reason = f"loss_limit ({sub_pnl_pct:.2f}%)"
            elif sub_pnl_pct >= daily_profit_pct:
                trigger_reason = f"profit_target ({sub_pnl_pct:.2f}%)"

            if trigger_reason:
                subsession_stats.append({
                    "day":        bar_day,
                    "slot_start": str(ts_dt - pd.Timedelta(minutes=subsession_cooldown_min * 10)),
                    "slot_end":   ts,
                    "reason":     trigger_reason,
                    "pnl_pct":    round(sub_pnl_pct, 3),
                })
                resume_after_ts = ts_dt + cooldown   # クールダウン開始

        # --- Manage open position ---
        if position is not None:
            sl   = position["stop_loss"]
            tp   = position["take_profit"]
            side = position["side"]   # "long" or "short"
            exit_price  = None
            exit_reason = None

            # JPParabolicSwing psar_15m: 足ごとの PSAR でトレーリング SL を更新
            sl_mode = (strategy.meta.params or {}).get("sl_mode", "")
            if (
                side == "long"
                and str(sl_mode).lower() == "psar_15m"
                and "psar_15m" in row.index
            ):
                p15 = row["psar_15m"]
                if not np.isnan(p15):
                    if np.isnan(sl):
                        sl = float(p15)
                    else:
                        sl = max(float(sl), float(p15))
                    position["stop_loss"] = sl

            # EOD強制クローズ（JP株1日信用対応）
            if eod_close_time is not None and exit_price is None:
                bar_h = ts_dt.hour
                bar_m = ts_dt.minute
                if (bar_h > eod_close_time[0] or
                        (bar_h == eod_close_time[0] and bar_m >= eod_close_time[1])):
                    exit_price, exit_reason = price, "eod_close"

            if side == "long":
                # ロング: 安値がSL以下 → 損切 / 高値がTP以上 → 利確
                if exit_price is None:
                    if not np.isnan(sl) and row["low"] <= sl:
                        exit_price, exit_reason = sl, "stop_loss"
                    elif not np.isnan(tp) and row["high"] >= tp:
                        exit_price, exit_reason = tp, "take_profit"
                    elif prev["signal"] in (-1, -2):   # 売りシグナル or 強制決済
                        exit_price, exit_reason = price, "signal"
            else:  # short
                # ショート: 高値がSL以上 → 損切 / 安値がTP以下 → 利確
                if exit_price is None:
                    if not np.isnan(sl) and row["high"] >= sl:
                        exit_price, exit_reason = sl, "stop_loss"
                    elif not np.isnan(tp) and row["low"] <= tp:
                        exit_price, exit_reason = tp, "take_profit"
                    elif prev["signal"] in (1, -1):    # 買いシグナル or 強制決済
                        exit_price, exit_reason = price, "signal"

            if exit_price is not None:
                if side == "long":
                    exit_net = exit_price * (1 - fee_pct)
                    # Phase D4: 信用買い金利 — 保有日数×年率で按分してコスト化
                    bars_per_day_long = {
                        "1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7,
                        "1d": 1, "1wk": 1 / 5, "1mo": 1 / 21,
                    }.get(strategy.meta.interval, 78)
                    hold_bars_long = i - position["entry_bar"]
                    hold_days_long = max(hold_bars_long / bars_per_day_long, 1 / bars_per_day_long)
                    margin_cost = (
                        position["entry_net"] * position["qty"]
                        * (long_margin_interest_annual * hold_days_long / 365)
                    ) if long_margin_interest_annual > 0 else 0.0
                    pnl      = (exit_net - position["entry_net"]) * position["qty"] - margin_cost
                    pnl_pct  = (exit_net / position["entry_net"] - 1) * 100
                else:  # short: 売値 - 買戻し値 - 貸株料 - プレミアム料
                    exit_net = exit_price * (1 + fee_pct)
                    # 貸株料: 保有時間（バー数）から日数換算して年率から按分
                    hold_bars   = i - position["entry_bar"]
                    # Phase C4: 日足/週足/月足のバー数換算を追加（スイング backtest 対応）
                    bars_per_day = {
                        "1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7,
                        "1d": 1, "1wk": 1 / 5, "1mo": 1 / 21,
                    }.get(strategy.meta.interval, 78)
                    hold_days   = max(hold_bars / bars_per_day, 1/bars_per_day)  # 最低1バー分
                    borrow_cost = position["entry_net"] * position["qty"] * (short_borrow_fee_annual * hold_days / 365)
                    # プレミアム料: 日割り（デイトレでも1日分かかる）
                    premium_cost = position["entry_net"] * position["qty"] * short_premium_daily_pct if short_premium_daily_pct > 0 else 0
                    pnl         = (position["entry_net"] - exit_net) * position["qty"] - borrow_cost - premium_cost
                    pnl_pct     = (position["entry_net"] / exit_net - 1) * 100
                equity  += pnl

                trades.append(Trade(
                    entry_time    = position["entry_time"],
                    exit_time     = ts,
                    symbol        = strategy.meta.symbol,
                    side          = side,
                    entry_price   = position["entry_price"],
                    exit_price    = exit_price,
                    qty           = position["qty"],
                    pnl           = pnl,
                    pnl_pct       = pnl_pct,
                    exit_reason   = exit_reason,
                    duration_bars = i - position["entry_bar"],
                ))
                position = None

        # --- Pyramid: 保有中にsignal=2が来たら追加（ロング・ショート両対応） ---
        max_pyramid = getattr(strategy.meta, "max_pyramid", 0)
        if (max_pyramid > 0
                and position is not None
                and position.get("pyramid_count", 0) < max_pyramid
                and prev["signal"] == 2
                and resume_after_ts is None):
            add_price = row["open"]
            if position["side"] == "long":
                add_net = add_price * (1 + fee_pct)
            else:
                add_net = add_price * (1 - fee_pct)
            add_raw = (equity * position_pct) / add_price
            if lot_size > 1:
                add_qty = int(add_raw // lot_size) * lot_size
            else:
                add_qty = add_raw
            if (lot_size <= 1 and add_qty > 0) or (lot_size > 1 and add_qty >= lot_size):
                old_qty = position["qty"]
                new_qty = old_qty + add_qty
                # 加重平均エントリーコスト
                position["entry_net"] = (
                    position["entry_net"] * old_qty + add_net * add_qty
                ) / new_qty
                position["qty"]           = new_qty
                position["pyramid_count"] = position.get("pyramid_count", 0) + 1
                # SLをブレークイーブンに引き上げて初回エントリーを保護
                if not np.isnan(position.get("stop_loss") or np.nan):
                    if position["side"] == "long":
                        position["stop_loss"] = max(
                            position["stop_loss"], position["entry_net"]
                        )
                    else:  # short: SLを引き下げ（コスト以下にはしない）
                        position["stop_loss"] = min(
                            position["stop_loss"], position["entry_net"]
                        )

        # --- Check for entry (クールダウン中はエントリーしない) ---
        # signal=1: 買いエントリー  signal=-2: 売りエントリー
        if position is None and resume_after_ts is None and prev["signal"] in (1, -2):
            is_long     = prev["signal"] == 1
            limit_price = prev["close"]

            # 指値スルー判定
            if limit_slip_pct > 0:
                gap = abs(row["open"] - limit_price) / limit_price
                if gap > limit_slip_pct:
                    continue

            entry_price = row["open"]
            if entry_price != entry_price or entry_price <= 0:  # NaN or invalid
                continue
            # 動的ロット倍率（戦略からlot_multiplier列が提供されていれば使用）
            lot_mult = float(prev.get("lot_multiplier", 1.0)) if "lot_multiplier" in prev.index else 1.0
            if lot_mult != lot_mult or lot_mult <= 0:  # NaN guard
                lot_mult = 1.0
            effective_pct = min(position_pct * lot_mult, 0.95)  # 余力の95%を超えない
            qty_raw = (equity * effective_pct) / entry_price
            if qty_raw != qty_raw:  # NaN guard
                continue
            if lot_size > 1:
                qty = int(qty_raw // lot_size) * lot_size
                if qty < lot_size:
                    continue
            else:
                qty = qty_raw

            # Phase E2: 出来高参加率スリッページ — 大口発注ほど不利方向に価格がずれる。
            # slippage_pct = coeff * (qty / bar_volume). ロングは上方、ショートは下方に約定。
            vol_slip = 0.0
            if volume_impact_coeff > 0 and "volume" in row.index:
                bar_vol = float(row.get("volume") or 0.0)
                if bar_vol > 0 and qty > 0:
                    participation = min(qty / bar_vol, 0.5)  # 50%参加超は打ち止め
                    vol_slip = volume_impact_coeff * participation

            if is_long:
                entry_net = entry_price * (1 + fee_pct + vol_slip)
            else:
                entry_net = entry_price * (1 - fee_pct - vol_slip)   # 売り建て: 不利方向

            sl = prev["stop_loss"]
            tp = prev["take_profit"]
            position = {
                "entry_time":  ts,
                "entry_price": entry_price,
                "entry_net":   entry_net,
                "qty":         qty,
                "side":        "long" if is_long else "short",
                "stop_loss":   sl if not np.isnan(sl)  else np.nan,
                "take_profit": tp if not np.isnan(tp)  else np.nan,
                "entry_bar":   i,
            }

        equity_curve.append(equity)

    # Force-close any open position at end
    if position is not None:
        last_price = sig_df.iloc[-1]["close"]
        side = position["side"]
        if side == "long":
            exit_net = last_price * (1 - fee_pct)
            pnl      = (exit_net - position["entry_net"]) * position["qty"]
            pnl_pct  = (exit_net / position["entry_net"] - 1) * 100
        else:
            exit_net = last_price * (1 + fee_pct)
            pnl      = (position["entry_net"] - exit_net) * position["qty"]
            pnl_pct  = (position["entry_net"] / exit_net - 1) * 100
        equity += pnl
        trades.append(Trade(
            entry_time    = position["entry_time"],
            exit_time     = str(sig_df.index[-1]),
            symbol        = strategy.meta.symbol,
            side          = side,
            entry_price   = position["entry_price"],
            exit_price    = last_price,
            qty           = position["qty"],
            pnl           = pnl,
            pnl_pct       = pnl_pct,
            exit_reason   = "end",
            duration_bars = len(sig_df) - position["entry_bar"],
        ))
        equity_curve.append(equity)

    result.trades          = trades
    result.equity_curve    = equity_curve
    result.subsession_stats = subsession_stats
    _compute_metrics(result, starting_cash, usd_jpy, df)
    return result


def _compute_metrics(result: BacktestResult, starting_cash: float,
                     usd_jpy: float, df: pd.DataFrame) -> None:
    trades = result.trades
    if not trades:
        return

    pnls     = [t.pnl for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p <= 0]

    result.num_trades        = len(trades)
    result.win_rate          = len(wins) / len(trades) * 100
    result.profit_factor     = (sum(wins) / -sum(losses)) if losses else 999.0
    result.total_return_pct  = (result.equity_curve[-1] / starting_cash - 1) * 100
    result.avg_trade_pct     = float(np.mean([t.pnl_pct for t in trades]))
    result.avg_duration_bars = float(np.mean([t.duration_bars for t in trades]))

    # Max drawdown
    eq = np.array(result.equity_curve)
    peak = np.maximum.accumulate(eq)
    dd   = (eq - peak) / peak * 100
    result.max_drawdown_pct = float(dd.min())

    # Daily P&L
    days = max((df.index[-1] - df.index[0]).days, 1)
    result.days_tested = days
    total_pnl_usd = sum(pnls)
    result.daily_pnl_usd = total_pnl_usd / days
    result.daily_pnl_jpy = result.daily_pnl_usd * usd_jpy
    result.gross_profit_jpy = (sum(wins)   * usd_jpy) / days
    result.gross_loss_jpy   = (sum(losses) * usd_jpy) / days
    result.avg_win_jpy  = float(np.mean(wins))   * usd_jpy if wins   else 0.0
    result.avg_loss_jpy = float(np.mean(losses)) * usd_jpy if losses else 0.0

    # Sharpe (annualized, using daily returns)
    if len(result.equity_curve) > 2:
        eq_series = pd.Series(result.equity_curve)
        daily_ret = eq_series.pct_change().dropna()
        if daily_ret.std() > 0:
            result.sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))

    # Phase A: 複利スケール指標（CAGR / Calmar / MAR / 日次リターン%）
    # 1億到達を見据えた promotion 判定のため、円/日ではなく %/日・CAGR を取り扱う
    try:
        final_equity = float(result.equity_curve[-1])
        if starting_cash > 0 and final_equity > 0 and days > 0:
            # CAGR: (final/start)^(365/days) - 1
            growth = final_equity / starting_cash
            if growth > 0:
                cagr = (growth ** (365.0 / days) - 1.0) * 100.0
            else:
                cagr = -100.0
            result.cagr = float(cagr)

        # バー単位の日次リターン%を集計（bar pct_change を日付でグループ化して日次化）
        # 単純化のため、equity_curve を日次リサンプリングした pct_change を使う
        if len(result.equity_curve) > 2 and len(df.index) == len(result.equity_curve) - 1:
            # equity_curve は「先頭に starting_cash + 各バー終端の equity」なので、df.index と1つずれる
            eq_daily = pd.Series(result.equity_curve[1:], index=df.index).resample("1D").last().dropna()
            daily_pct = eq_daily.pct_change().dropna() * 100.0
            if len(daily_pct) > 0:
                result.daily_return_pct_mean = float(daily_pct.mean())
                result.daily_return_pct_std  = float(daily_pct.std())

        # Calmar = CAGR / |MDD%|  （MDDが0に近い場合は上限を設定）
        mdd_abs = abs(result.max_drawdown_pct)
        if mdd_abs > 0.5:
            result.calmar = float(result.cagr / mdd_abs)
        else:
            result.calmar = float(result.cagr)   # 実質 DD なしのときは CAGR をそのまま採用
        # MAR は慣習的に Calmar と同義の別名として流通しているのでエイリアス
        result.mar = result.calmar
    except Exception:
        # 指標計算失敗は致命ではないのでデフォルト値のまま残す
        pass

    # Composite score: reward win_rate + profit_factor, penalize drawdown
    result.score = (
        result.win_rate * 0.3 +
        min(result.profit_factor, 5) * 10 +
        result.daily_pnl_jpy / 100 -
        abs(result.max_drawdown_pct) * 0.5
    )
