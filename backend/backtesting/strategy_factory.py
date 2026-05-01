"""StrategyFactory — (手法名, 銘柄, パラメータ) → 戦略インスタンスを生成."""
from __future__ import annotations

from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_micro_scalp import JPMicroScalp
from backend.strategies.jp_stock.enhanced_macd_rci import EnhancedMacdRci
from backend.strategies.jp_stock.enhanced_scalp import EnhancedScalp
from backend.strategies.jp_stock.jp_bb_short import JPBbShort
from backend.strategies.jp_stock.jp_ma_vol import JPMaVol
from backend.strategies.jp_stock.jp_pullback import JPPullback
from backend.strategies.jp_stock.jp_swing_donchian import JPSwingDonchianD
from backend.strategies.jp_stock.jp_parabolic_swing import JPParabolicSwing

# アーカイブ済み（PDCA 非対象）: Momentum5Min / ORB / VwapReversion
# いずれも experiments で平均 OOS が負 or NULL、robust=0 だったため、
# factory/daemon のループから外して新規研究リソースを温存する。
# クラス本体は backend/lab/runner.py や social_strategy.py が直接 import しているため残す。

# 全手法のデフォルトパラメータと許容範囲
STRATEGY_DEFAULTS = {
    "MacdRci": {
        "interval": "5m",
        "tp_pct": 0.003, "sl_pct": 0.001,
        "rci_min_agree": 1, "macd_signal": 9,
        "macd_fast": 3, "macd_slow": 7,
        "rci_entry_mode": 0,
        "rci_gc_slope_lookback": 3,
        "rci_gc_slope_enabled": 0,
        "rci_gc_slope_min": -999.0,
        "rci_gc_slope_max": 999.0,
        "entry_profile": 0, "exit_profile": 0,
        "hist_exit_delay_bars": 1, "rci_exit_min_agree": 2,
        # Phase F7 事故防止フィルタ（デフォルト OFF）
        "disable_lunch_session_entry": 0,
        "require_macd_above_signal": 0,
        "rci_danger_zone_enabled": 0,
        "rci_danger_low": -80.0,
        "rci_danger_high": 80.0,
        "volume_surge_max_ratio": 0.0,
        "volume_surge_lookback": 5,
        # F8 (2026-05-01): 寄付直後 short 禁止 (デフォルト OFF、PoC で個別 ON)
        "morning_first_30min_short_block": 0,
        "morning_block_until_min": 30,
    },
    "Breakout": {
        "interval": "5m",
        "tp_pct": 0.005, "sl_pct": 0.003,
        "lookback": 10, "trend_bars": 3, "vol_confirm_mult": 1.3,
    },
    "Scalp": {
        "interval": "5m",
        "tp_pct": 0.002, "sl_pct": 0.001,
        "ema_fast": 5, "ema_slow": 13,
        "atr_period": 10, "atr_min_pct": 0.001,
        "morning_only": True, "allow_short": True,
        "vwap_dev_limit": 0.005,
    },
    # 2026-04-30 ユーザー提案: +5円 1分以内 即決スキャル (アルゴ優位領域)
    "MicroScalp": {
        "interval": "1m",
        "tp_jpy": 5.0,
        "sl_jpy": 5.0,
        "entry_dev_jpy": 8.0,
        "atr_period": 10,
        "atr_min_jpy": 3.0,
        "atr_max_jpy": 0.0,
        "timeout_bars": 2,
        "cooldown_bars": 5,
        "avoid_open_min": 5,
        "avoid_close_min": 30,
        "morning_only": False,
        "allow_short": True,
        "max_trades_per_day": 0,
        # 2026-04-30 グリッド評価で 9:30-11:30 が擬陽性化することが判明。
        # デフォルトでは「寄り後 30 分 + 後場」のみ許可し、前場後半を除外。
        "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
        # 2026-04-30 寄り付きパターン分析と v4 検証で確認:
        #   - 仮説 (GD はショート優位 60-87%) は当たっていた (short_PnL +6.3% 改善)
        #   - しかし 7 日 60 サンプルでは銘柄横断の固定バイアスは過剰削減で総合 -9% (3103.T等)
        #   - 銘柄別の過去パターン履歴 (30 日以上) で個別判定するまで既定 OFF
        # 戦略実装は残し、長期 1m データを取得後に再評価する。
        "open_bias_mode": False,
        "bias_observe_min": 10,
        "bias_apply_until_min": 30,
        # 2026-04-30 D9 Phase 2: MTFRA フィルタ既定 OFF (バックテストで個別有効化)
        # "off" / "default" (3m+30m+60m) / "aggressive" (1m+3m+15m+60m) / "per_symbol"
        "mtfra_mode": "off",
    },
    # BB 上端(3σ)タッチでショートのみ（ロング新規なし）
    "BbShort": {
        "interval": "5m",
        "bb_period": 20,
        "bb_std": 3.0,
        "tp_pct": 0.004,
        "sl_pct": 0.002,
        "full_session": 1,
    },
    # 勝率優先トレンド押し目買い（ロング専用, 5m）
    # デフォルトは探索開始用にエントリー頻度を確保できる緩めの帯域。
    # 勝率が伸びる方向は PDCA で狭めていく。
    "Pullback": {
        "interval": "5m",
        "ema_fast": 20,
        "ema_slow": 50,
        "slope_lookback": 3,
        "pullback_lookback": 10,
        "pullback_depth_pct": 0.001,
        "vol_ma_period": 20,
        "vol_confirm_mult": 1.0,
        "vwap_tol_pct": 0.003,
        "rsi_period": 14,
        "rsi_min": 30.0,
        "rsi_max": 75.0,
        "tp_pct": 0.004,
        "sl_pct": 0.003,
        "full_session": 1,
    },
    # 日足スイング（Donchian 20 ブレイク + EMA50 + ATR）
    "SwingDonchianD": {
        "interval": "1d",
        "ema_slow": 50,
        "entry_lookback": 20,
        "exit_lookback": 10,
        "atr_period": 14,
        "sl_atr_mult": 2.0,
        "tp_atr_mult": 4.0,
        "vol_ma_period": 20,
        "vol_confirm_mult": 1.0,
    },
    # MA×出来高: interval_code → 1m,3m,5m / 15m,30m,1h（スイング・デイ/スキャの目安）
    # MTF: 15m 主足 + 日足/1H（バックテストでは extra_ohlcv で 1d/1h を attach）
    "ParabolicSwing": {
        "interval": "15m",
        "psar_af_start": 0.02,
        "psar_af_step": 0.02,
        "psar_af_max": 0.20,
        "rci_exit_threshold": 95.0,
        "rci_exit_min_agree": 1,
        "ma_exit_dev_pct": 0.05,
        "exit_logic": "or",
        "entry_rci_h1_max": 80.0,
        "min_hold_bars": 22,
        "entry_cooldown_bars": 22,
        "entry_mode": "trend_follow",
        "sl_mode": "psar_15m",
        "sl_pct_from_entry": 0.03,
        "max_hold_bars": 0,
        "trend_flip_lookback_bars": 1,
        "rci_entry_filter_period": 10,
        "ma_exit_period": 5,
    },
    "MaVol": {
        "interval_code": 2,
        "ema_fast": 9,
        "ema_slow": 21,
        "vol_ma_period": 20,
        "vol_confirm_mult": 1.2,
        "tp_pct": 0.004,
        "sl_pct": 0.002,
        "full_session": 1,
        "allow_short": 1,
        "vwap_entry_margin_pct": 0.0005,
    },
}

# パラメータ範囲 (min, max, type)
PARAM_RANGES = {
    "MacdRci": {
        "tp_pct":        (0.001, 0.015, float),  # 利を伸ばす方向に拡張
        "sl_pct":        (0.0005, 0.005, float),
        "rci_min_agree": (1, 3, int),
        "macd_signal":   (5, 15, int),
        "macd_fast":     (2, 7, int),
        "macd_slow":     (5, 15, int),
        # 手法内部PDCA用ロジック軸
        "entry_profile": (0, 2, int),
        "exit_profile":  (0, 2, int),
        "hist_exit_delay_bars": (1, 3, int),
        "rci_exit_min_agree": (1, 3, int),
        # 0=RCI多数決 / 1=最短×最長GC翌足 / 2=最短×最長GC当足
        "rci_entry_mode": (0, 2, int),
        "rci_gc_slope_lookback": (1, 12, int),
        "rci_gc_slope_enabled": (0, 1, int),
        "rci_gc_slope_min": (-30.0, 30.0, float),
        "rci_gc_slope_max": (-30.0, 30.0, float),
        # Phase F7 事故防止フィルタ（探索キー、すべて 0 含む = OFF を許容）
        "disable_lunch_session_entry": (0, 1, int),
        "require_macd_above_signal": (0, 1, int),
        "rci_danger_zone_enabled": (0, 1, int),
        # 危険帯はおおむね下側で観測されている（極オーバーソールド or 中途半端売られ）。
        # 上側 80 以上もあるため high は 0..80 で振る。low と high は内部で swap される。
        "rci_danger_low": (-100.0, 0.0, float),
        "rci_danger_high": (-80.0, 80.0, float),
        # 0 = 無効、>0 = 有効化。3.0 を上限にすると 1.5x〜3.0x が当たりやすい。
        "volume_surge_max_ratio": (0.0, 3.0, float),
    },
    "Scalp": {
        "tp_pct":   (0.001, 0.008, float),  # 拡張
        "sl_pct":   (0.0005, 0.003, float),
        "ema_fast": (2, 8, int),
        "ema_slow": (8, 20, int),
        "atr_period": (6, 20, int),
        "atr_min_pct": (0.0005, 0.003, float),
        "vwap_dev_limit": (0.002, 0.010, float),
        "morning_only": (0, 1, int),
        "allow_short": (0, 1, int),
    },
    "Breakout": {
        "tp_pct": (0.003, 0.020, float),  # 拡張
        "sl_pct": (0.001, 0.008, float),
        "lookback": (6, 20, int),
        "trend_bars": (2, 6, int),
        "vol_confirm_mult": (1.0, 2.0, float),
    },
    "EnhancedScalp": {
        "tp_pct":   (0.002, 0.008, float),
        "sl_pct":   (0.001, 0.004, float),
        "ema_fast": (2, 8, int),
        "ema_slow": (8, 20, int),
        "bb_period": (15, 30, int),
        "bb_std":   (1.5, 3.0, float),
        "rsi_period": (7, 20, int),
        "rsi_exit_high": (65, 80, float),
        "max_pyramid": (0, 2, int),
    },
    "EnhancedMacdRci": {
        "tp_pct":        (0.005, 0.015, float),
        "sl_pct":        (0.001, 0.005, float),
        "rci_min_agree": (1, 3, int),
        "macd_signal":   (5, 15, int),
        "macd_fast":     (2, 7, int),
        "macd_slow":     (5, 15, int),
        "bb_period":     (15, 40, int),
        "bb_std":        (2.0, 3.5, float),
        "rsi_period":    (7, 20, int),
        "rsi_exit_high": (65, 80, float),
        "vwap_stop": (0, 1, int),
        "allow_reentry": (0, 1, int),
        "max_pyramid": (0, 2, int),
    },
    "MaVol": {
        # Phase C4: 日足（code=6）まで許容し、日足スイングを探索対象に入れる
        "interval_code": (0, 6, int),
        "ema_fast": (3, 15, int),
        "ema_slow": (18, 55, int),
        "vol_ma_period": (10, 40, int),
        "vol_confirm_mult": (1.05, 1.8, float),
        "tp_pct": (0.002, 0.040, float),   # 日足想定で利確幅を拡張
        "sl_pct": (0.001, 0.020, float),   # 日足想定で損切幅を拡張
        "full_session": (0, 1, int),
        "allow_short": (0, 1, int),
        "vwap_entry_margin_pct": (0.0, 0.003, float),
    },
    "BbShort": {
        "bb_period": (14, 40, int),
        "bb_std": (2.5, 3.5, float),
        "tp_pct": (0.002, 0.012, float),
        "sl_pct": (0.001, 0.006, float),
        "full_session": (0, 1, int),
    },
    # 勝率優先のトレンド押し目買い。rsi_min/max の帯幅で勝率と機会のトレードオフを探索
    "Pullback": {
        "ema_fast": (10, 30, int),
        "ema_slow": (30, 80, int),
        "slope_lookback": (1, 8, int),
        "pullback_lookback": (3, 15, int),
        "pullback_depth_pct": (0.0005, 0.005, float),
        "vol_confirm_mult": (1.0, 1.8, float),
        "vwap_tol_pct": (0.0, 0.005, float),
        "rsi_period": (7, 20, int),
        "rsi_min": (25.0, 50.0, float),
        "rsi_max": (55.0, 80.0, float),
        "tp_pct": (0.002, 0.010, float),
        "sl_pct": (0.001, 0.005, float),
        "full_session": (0, 1, int),
    },
    "ParabolicSwing": {
        "psar_af_start": (0.01, 0.04, float),
        "psar_af_step": (0.01, 0.04, float),
        "psar_af_max": (0.10, 0.30, float),
        "rci_exit_threshold": (85.0, 99.0, float),
        "rci_exit_min_agree": (1, 3, int),
        "ma_exit_dev_pct": (0.02, 0.12, float),
        "entry_rci_h1_max": (65.0, 95.0, float),
        "min_hold_bars": (11, 44, int),
        "entry_cooldown_bars": (11, 44, int),
        "sl_pct_from_entry": (0.015, 0.05, float),
        "trend_flip_lookback_bars": (1, 4, int),
        "rci_entry_filter_period": (7, 14, int),
        "ma_exit_period": (3, 10, int),
    },
    # 日足スイング。Donchian 窓と ATR 倍率を探索。
    "SwingDonchianD": {
        "ema_slow": (20, 100, int),
        "entry_lookback": (10, 40, int),
        "exit_lookback": (5, 20, int),
        "atr_period": (7, 28, int),
        "sl_atr_mult": (1.0, 4.0, float),
        "tp_atr_mult": (2.0, 8.0, float),
        "vol_confirm_mult": (0.8, 1.6, float),
    },
}

STRATEGY_DEFAULTS["EnhancedScalp"] = {
    "interval": "5m",
    "tp_pct": 0.004, "sl_pct": 0.002,
    "ema_fast": 5, "ema_slow": 13,
    "bb_period": 20, "bb_std": 2.0,
    "rsi_period": 14, "rsi_exit_high": 70,
    "max_pyramid": 1,
}

STRATEGY_DEFAULTS["EnhancedMacdRci"] = {
    "interval": "5m",
    "tp_pct": 0.009, "sl_pct": 0.003,
    "rci_min_agree": 1, "macd_signal": 9,
    "macd_fast": 2, "macd_slow": 10,
    "bb_period": 30, "bb_std": 3.0,
    "rsi_period": 14, "rsi_exit_high": 70,
    "vwap_stop": True, "allow_reentry": True,
    "max_pyramid": 1,
}

# MaVol: code → yfinance 互換ラベル（JP 株 intraday / Phase C4: daily swing）
MAVOL_INTERVAL_BY_CODE: dict[int, str] = {
    0: "1m",
    1: "3m",
    2: "5m",
    3: "15m",
    4: "30m",
    5: "1h",
    6: "1d",   # Phase C4: 日足スイング（長期データで MaVol を回す）
}


def resolve_jp_ohlcv_interval(strategy_name: str, params: dict | None) -> str:
    """バックテスト用 OHLCV の時間足。MaVol は ``interval_code`` で決定。"""
    p = {**STRATEGY_DEFAULTS.get(strategy_name, {}), **(params or {})}
    if strategy_name == "MaVol":
        code = int(p.get("interval_code", 2))
        return MAVOL_INTERVAL_BY_CODE.get(code, "5m")
    if strategy_name == "SwingDonchianD":
        return "1d"
    if strategy_name == "ParabolicSwing":
        return str(p.get("interval", "15m"))
    return str(p.get("interval", "5m"))


ALL_STRATEGY_NAMES = list(STRATEGY_DEFAULTS.keys())


def create(strategy_name: str, symbol: str, name: str = "",
           params: dict | None = None, interval: str = "5m"):
    """手法名とパラメータから戦略インスタンスを生成する。"""
    if not name:
        name = symbol.replace(".T", "")
    p = {**STRATEGY_DEFAULTS.get(strategy_name, {}), **(params or {})}

    if strategy_name == "MacdRci":
        return JPMacdRci(
            symbol, name, interval=p.get("interval", interval),
            macd_fast=p.get("macd_fast", 3),
            macd_slow=p.get("macd_slow", 7),
            macd_signal=p.get("macd_signal", 9),
            rci_min_agree=p.get("rci_min_agree", 1),
            tp_pct=p.get("tp_pct", 0.003),
            sl_pct=p.get("sl_pct", 0.001),
            entry_profile=p.get("entry_profile", 0),
            exit_profile=p.get("exit_profile", 0),
            hist_exit_delay_bars=p.get("hist_exit_delay_bars", 1),
            rci_exit_min_agree=p.get("rci_exit_min_agree", 2),
            rci_entry_mode=int(p.get("rci_entry_mode", 0)),
            rci_gc_slope_lookback=int(p.get("rci_gc_slope_lookback", 3)),
            rci_gc_slope_enabled=int(p.get("rci_gc_slope_enabled", 0)),
            rci_gc_slope_min=float(p.get("rci_gc_slope_min", -999.0)),
            rci_gc_slope_max=float(p.get("rci_gc_slope_max", 999.0)),
            # Phase F7 事故防止フィルタ（opt-in、デフォルト OFF で既存挙動を温存）
            disable_lunch_session_entry=int(p.get("disable_lunch_session_entry", 0)),
            require_macd_above_signal=int(p.get("require_macd_above_signal", 0)),
            rci_danger_zone_enabled=int(p.get("rci_danger_zone_enabled", 0)),
            rci_danger_low=float(p.get("rci_danger_low", -80.0)),
            rci_danger_high=float(p.get("rci_danger_high", 80.0)),
            volume_surge_max_ratio=float(p.get("volume_surge_max_ratio", 0.0)),
            volume_surge_lookback=int(p.get("volume_surge_lookback", 5)),
            morning_first_30min_short_block=int(p.get("morning_first_30min_short_block", 0)),
            morning_block_until_min=int(p.get("morning_block_until_min", 30)),
            max_pyramid=int(p.get("max_pyramid", 0)),
        )
    elif strategy_name == "Breakout":
        return JPBreakout(
            symbol, name, interval=p.get("interval", interval),
            lookback=p.get("lookback", 10),
            tp_pct=p.get("tp_pct", 0.005),
            sl_pct=p.get("sl_pct", 0.003),
            trend_bars=p.get("trend_bars", 3),
            vol_confirm_mult=p.get("vol_confirm_mult", 1.3),
        )
    elif strategy_name == "Scalp":
        return JPScalp(
            symbol, name, interval=p.get("interval", interval),
            ema_fast=p.get("ema_fast", 5),
            ema_slow=p.get("ema_slow", 13),
            tp_pct=p.get("tp_pct", 0.002),
            sl_pct=p.get("sl_pct", 0.001),
            atr_period=p.get("atr_period", 10),
            atr_min_pct=p.get("atr_min_pct", 0.001),
            morning_only=bool(p.get("morning_only", True)),
            allow_short=bool(p.get("allow_short", True)),
            vwap_dev_limit=p.get("vwap_dev_limit", 0.005),
        )
    elif strategy_name == "MicroScalp":
        return JPMicroScalp(
            symbol, name, interval=p.get("interval", "1m"),
            tp_jpy=float(p.get("tp_jpy", 5.0)),
            sl_jpy=float(p.get("sl_jpy", 5.0)),
            entry_dev_jpy=float(p.get("entry_dev_jpy", 8.0)),
            atr_period=int(p.get("atr_period", 10)),
            atr_min_jpy=float(p.get("atr_min_jpy", 3.0)),
            atr_max_jpy=float(p.get("atr_max_jpy", 0.0)),
            timeout_bars=int(p.get("timeout_bars", 2)),
            cooldown_bars=int(p.get("cooldown_bars", 5)),
            avoid_open_min=int(p.get("avoid_open_min", 5)),
            avoid_close_min=int(p.get("avoid_close_min", 30)),
            morning_only=bool(p.get("morning_only", False)),
            allow_short=bool(p.get("allow_short", True)),
            max_trades_per_day=int(p.get("max_trades_per_day", 0)),
            allowed_time_windows=p.get("allowed_time_windows") or None,
            open_bias_mode=bool(p.get("open_bias_mode", False)),
            bias_observe_min=int(p.get("bias_observe_min", 10)),
            bias_apply_until_min=int(p.get("bias_apply_until_min", 30)),
            mtfra_mode=str(p.get("mtfra_mode", "off") or "off"),
        )
    elif strategy_name == "EnhancedScalp":
        return EnhancedScalp(
            symbol, name, interval=p.get("interval", interval),
            ema_fast=p.get("ema_fast", 5),
            ema_slow=p.get("ema_slow", 13),
            tp_pct=p.get("tp_pct", 0.004),
            sl_pct=p.get("sl_pct", 0.002),
            bb_period=p.get("bb_period", 20),
            bb_std=p.get("bb_std", 2.0),
            rsi_period=p.get("rsi_period", 14),
            rsi_exit_high=p.get("rsi_exit_high", 70),
            max_pyramid=int(p.get("max_pyramid", 1)),
        )
    elif strategy_name == "EnhancedMacdRci":
        return EnhancedMacdRci(
            symbol, name, interval=p.get("interval", interval),
            macd_fast=p.get("macd_fast", 2),
            macd_slow=p.get("macd_slow", 10),
            macd_signal=p.get("macd_signal", 9),
            rci_min_agree=p.get("rci_min_agree", 1),
            tp_pct=p.get("tp_pct", 0.009),
            sl_pct=p.get("sl_pct", 0.003),
            bb_period=p.get("bb_period", 30),
            bb_std=p.get("bb_std", 3.0),
            rsi_period=p.get("rsi_period", 14),
            rsi_exit_high=p.get("rsi_exit_high", 70),
            vwap_stop=bool(p.get("vwap_stop", True)),
            allow_reentry=bool(p.get("allow_reentry", True)),
            max_pyramid=int(p.get("max_pyramid", 1)),
        )
    elif strategy_name == "BbShort":
        return JPBbShort(
            symbol,
            name,
            interval=p.get("interval", interval),
            bb_period=int(p.get("bb_period", 20)),
            bb_std=float(p.get("bb_std", 3.0)),
            tp_pct=float(p.get("tp_pct", 0.004)),
            sl_pct=float(p.get("sl_pct", 0.002)),
            full_session=bool(int(p.get("full_session", 1))),
        )
    elif strategy_name == "Pullback":
        return JPPullback(
            symbol,
            name,
            interval=p.get("interval", interval),
            ema_fast=int(p.get("ema_fast", 20)),
            ema_slow=int(p.get("ema_slow", 50)),
            slope_lookback=int(p.get("slope_lookback", 3)),
            pullback_lookback=int(p.get("pullback_lookback", 6)),
            pullback_depth_pct=float(p.get("pullback_depth_pct", 0.0015)),
            vol_ma_period=int(p.get("vol_ma_period", 20)),
            vol_confirm_mult=float(p.get("vol_confirm_mult", 1.1)),
            vwap_tol_pct=float(p.get("vwap_tol_pct", 0.002)),
            rsi_period=int(p.get("rsi_period", 14)),
            rsi_min=float(p.get("rsi_min", 35.0)),
            rsi_max=float(p.get("rsi_max", 65.0)),
            tp_pct=float(p.get("tp_pct", 0.004)),
            sl_pct=float(p.get("sl_pct", 0.003)),
            full_session=bool(int(p.get("full_session", 1))),
        )
    elif strategy_name == "SwingDonchianD":
        return JPSwingDonchianD(
            symbol,
            name,
            interval="1d",
            ema_slow=int(p.get("ema_slow", 50)),
            entry_lookback=int(p.get("entry_lookback", 20)),
            exit_lookback=int(p.get("exit_lookback", 10)),
            atr_period=int(p.get("atr_period", 14)),
            sl_atr_mult=float(p.get("sl_atr_mult", 2.0)),
            tp_atr_mult=float(p.get("tp_atr_mult", 4.0)),
            vol_ma_period=int(p.get("vol_ma_period", 20)),
            vol_confirm_mult=float(p.get("vol_confirm_mult", 1.0)),
        )
    elif strategy_name == "MaVol":
        ic = int(p.get("interval_code", 2))
        iv = MAVOL_INTERVAL_BY_CODE.get(ic, "5m")
        return JPMaVol(
            symbol,
            name,
            interval=iv,
            ema_fast=int(p.get("ema_fast", 9)),
            ema_slow=int(p.get("ema_slow", 21)),
            vol_ma_period=int(p.get("vol_ma_period", 20)),
            vol_confirm_mult=float(p.get("vol_confirm_mult", 1.2)),
            tp_pct=float(p.get("tp_pct", 0.004)),
            sl_pct=float(p.get("sl_pct", 0.002)),
            full_session=bool(int(p.get("full_session", 1))),
            interval_code=ic,
            allow_short=bool(int(p.get("allow_short", 1))),
            vwap_entry_margin_pct=float(p.get("vwap_entry_margin_pct", 0.0005)),
        )
    elif strategy_name == "ParabolicSwing":
        rp = p.get("rci_periods", (10, 12, 15))
        if isinstance(rp, list):
            rp = tuple(int(x) for x in rp)
        else:
            rp = tuple(rp)
        return JPParabolicSwing(
            symbol,
            name,
            interval=str(p.get("interval", "15m")),
            psar_af_start=float(p.get("psar_af_start", 0.02)),
            psar_af_step=float(p.get("psar_af_step", 0.02)),
            psar_af_max=float(p.get("psar_af_max", 0.20)),
            rci_periods=rp,
            rci_entry_filter_period=int(p.get("rci_entry_filter_period", 10)),
            rci_exit_threshold=float(p.get("rci_exit_threshold", 95.0)),
            rci_exit_min_agree=int(p.get("rci_exit_min_agree", 1)),
            ma_exit_period=int(p.get("ma_exit_period", 5)),
            ma_exit_dev_pct=float(p.get("ma_exit_dev_pct", 0.05)),
            exit_logic=str(p.get("exit_logic", "or")),
            overshoot_pct=float(p.get("overshoot_pct", 0.05)),
            entry_offset_pct=float(p.get("entry_offset_pct", 0.01)),
            arming_lookback_bars=int(p.get("arming_lookback_bars", 16)),
            require_15m_psar_up=bool(p.get("require_15m_psar_up", True)),
            entry_psar_source=str(p.get("entry_psar_source", "15m")),
            sl_mode=str(p.get("sl_mode", "psar_15m")),
            sl_pct_from_entry=float(p.get("sl_pct_from_entry", 0.03)),
            max_hold_bars=int(p.get("max_hold_bars", 0)),
            entry_cooldown_bars=int(p.get("entry_cooldown_bars", 22)),
            min_hold_bars=int(p.get("min_hold_bars", 22)),
            entry_rci_h1_max=float(p.get("entry_rci_h1_max", 80.0)),
            entry_mode=str(p.get("entry_mode", "trend_follow")),
            trend_flip_lookback_bars=int(p.get("trend_flip_lookback_bars", 1)),
        )
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")
