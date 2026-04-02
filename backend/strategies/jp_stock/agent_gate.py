"""エージェントゲート — バックテスト用の代理ルール実装.

エントリーシグナルが出た時に「本当に実行すべきか」を
複数の市場コンテキスト条件でフィルタリングする。

必須条件（両方 OK でなければ見送り）:
  A: トレンド方向一致  — SMA20/SMA5 の位置関係
  B: 最低出来高確保   — 出来高が直近平均の 50% 以上（流動性チェック）

加点条件（必須数をクリアすれば OK）:
  C: 出来高スパイク   — 直近平均の 1.3 倍以上（コンビクション）
  D: 時間帯適性       — 9:15〜14:45（始値/終値の乱れを除外）
  E: 直近モメンタム   — 直近 N 本が方向一致（押し目でなく流れに乗る）
  F: ローソク足シグナル — 最終足に逆方向の強いシグナルがない

設定デフォルト: A AND B AND (C or D or E or F のうち 1 つ以上)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime

import numpy as np
import pandas as pd


@dataclass
class GateResult:
    go: bool                    # True = エントリー実行
    a_trend: bool               # 必須: トレンド方向一致
    b_volume_min: bool          # 必須: 最低出来高
    c_volume_spike: bool        # 加点: 出来高スパイク
    d_time_window: bool         # 加点: 時間帯適性
    e_momentum: bool            # 加点: 直近モメンタム一致
    f_candle: bool              # 加点: ローソク足シグナル
    additive_score: int         # 加点条件の通過数
    reason: str                 # ゲート判定の理由（ログ用）

    def to_dict(self) -> dict:
        return {
            "go": self.go,
            "a_trend": self.a_trend,
            "b_volume_min": self.b_volume_min,
            "c_volume_spike": self.c_volume_spike,
            "d_time_window": self.d_time_window,
            "e_momentum": self.e_momentum,
            "f_candle": self.f_candle,
            "additive_score": self.additive_score,
            "reason": self.reason,
        }


class AgentGate:
    """バックテスト用エントリーゲート.

    Args:
        sma_fast:        短期SMA期間（デフォルト 5）
        sma_slow:        中期SMA期間（デフォルト 20）
        vol_ma_period:   出来高移動平均期間（デフォルト 20）
        vol_min_ratio:   必須出来高比率（デフォルト 0.5）
        vol_spike_ratio: スパイク判定比率（デフォルト 1.3）
        time_start:      有効時間帯 開始（デフォルト 09:15）
        time_end:        有効時間帯 終了（デフォルト 14:45）
        momentum_bars:   モメンタム判定バー数（デフォルト 3）
        additive_needed: 加点条件の必要通過数（デフォルト 1）
    """

    def __init__(
        self,
        sma_fast: int = 5,
        sma_slow: int = 20,
        vol_ma_period: int = 20,
        vol_min_ratio: float = 0.5,
        vol_spike_ratio: float = 1.3,
        time_start: tuple[int, int] = (9, 15),
        time_end: tuple[int, int] = (14, 45),
        momentum_bars: int = 3,
        additive_needed: int = 1,
    ):
        self.sma_fast = sma_fast
        self.sma_slow = sma_slow
        self.vol_ma_period = vol_ma_period
        self.vol_min_ratio = vol_min_ratio
        self.vol_spike_ratio = vol_spike_ratio
        self.time_start = dtime(*time_start)
        self.time_end = dtime(*time_end)
        self.momentum_bars = momentum_bars
        self.additive_needed = additive_needed

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        """ゲート判定に必要な列を事前計算して付与する。

        generate_signals() の後・engine の前に一度だけ呼ぶ。
        """
        d = df.copy()
        d["_sma_fast"]  = d["close"].rolling(self.sma_fast).mean()
        d["_sma_slow"]  = d["close"].rolling(self.sma_slow).mean()
        d["_vol_ma"]    = d["volume"].rolling(self.vol_ma_period).mean()
        return d

    def check(self, df: pd.DataFrame, idx: int, signal: int) -> GateResult:
        """1 本のバーについてゲートを評価する。

        Args:
            df:     precompute() 済みの DataFrame
            idx:    評価するバーのインデックス（整数位置）
            signal: 1=ロングエントリー候補, -2=ショートエントリー候補

        Returns:
            GateResult
        """
        if signal not in (1, -2):
            return GateResult(
                go=False, a_trend=False, b_volume_min=False,
                c_volume_spike=False, d_time_window=False,
                e_momentum=False, f_candle=False,
                additive_score=0, reason="non-entry signal"
            )

        row      = df.iloc[idx]
        is_long  = (signal == 1)
        close    = float(row["close"])
        vol      = float(row.get("volume", 0))
        sma_fast = float(row.get("_sma_fast", np.nan))
        sma_slow = float(row.get("_sma_slow", np.nan))
        vol_ma   = float(row.get("_vol_ma", np.nan))

        # ── 必須条件 A: トレンド方向一致 ─────────────────────────────────────
        # 条件A: 緩和版（close > SMA20のみ）
        if np.isnan(sma_slow):
            a_trend = False
        elif is_long:
            a_trend = close > sma_slow
        else:
            a_trend = close < sma_slow

        # ── 必須条件 B: 最低出来高 ────────────────────────────────────────────
        if np.isnan(vol_ma) or vol_ma == 0:
            b_volume_min = True  # データ不足時は通過扱い
        else:
            b_volume_min = vol >= vol_ma * self.vol_min_ratio

        # ── 加点条件 C: 出来高スパイク ────────────────────────────────────────
        if np.isnan(vol_ma) or vol_ma == 0:
            c_volume_spike = False
        else:
            c_volume_spike = vol >= vol_ma * self.vol_spike_ratio

        # ── 加点条件 D: 時間帯適性 ────────────────────────────────────────────
        try:
            bar_time = row.name.time() if hasattr(row.name, "time") else None
            d_time_window = (
                bar_time is not None
                and self.time_start <= bar_time <= self.time_end
            )
        except Exception:
            d_time_window = True  # 判定できない場合は通過

        # ── 加点条件 E: 直近モメンタム一致 ───────────────────────────────────
        e_momentum = False
        if idx >= self.momentum_bars:
            recent = df.iloc[idx - self.momentum_bars: idx]
            if is_long:
                # 直近 N 本の終値が上昇傾向（最後が最初より高い、かつ全体の半数以上が陽線）
                bullish_bars = (recent["close"] > recent["open"]).sum()
                e_momentum = (
                    float(recent["close"].iloc[-1]) > float(recent["close"].iloc[0])
                    and bullish_bars >= self.momentum_bars // 2 + 1
                )
            else:
                bearish_bars = (recent["close"] < recent["open"]).sum()
                e_momentum = (
                    float(recent["close"].iloc[-1]) < float(recent["close"].iloc[0])
                    and bearish_bars >= self.momentum_bars // 2 + 1
                )

        # ── 加点条件 F: ローソク足シグナル（逆方向の強いシグナルがない）────────
        f_candle = False
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            body = abs(close - o)
            if body > 0:
                if is_long:
                    upper_wick = h - max(close, o)
                    # 上ヒゲがボディの 2 倍未満 → 売り圧力が強くない
                    f_candle = upper_wick < body * 2.0
                else:
                    lower_wick = min(close, o) - l
                    f_candle = lower_wick < body * 2.0
            else:
                f_candle = True  # 同値足は中立
        except Exception:
            f_candle = True

        # ── ゲート判定 ────────────────────────────────────────────────────────
        additive_score = sum([c_volume_spike, d_time_window, e_momentum, f_candle])
        mandatory_ok   = a_trend and b_volume_min
        additive_ok    = additive_score >= self.additive_needed
        go             = mandatory_ok and additive_ok

        parts = []
        if not a_trend:      parts.append("A(trend)NG")
        if not b_volume_min: parts.append("B(vol_min)NG")
        if not additive_ok:  parts.append(f"additive={additive_score}/{self.additive_needed}NG")
        reason = "GO" if go else " / ".join(parts)

        return GateResult(
            go=go,
            a_trend=a_trend, b_volume_min=b_volume_min,
            c_volume_spike=c_volume_spike, d_time_window=d_time_window,
            e_momentum=e_momentum, f_candle=f_candle,
            additive_score=additive_score,
            reason=reason,
        )

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """シグナル列にゲートを適用した DataFrame を返す。

        engine.py に渡す前に呼ぶ。
        - ゲートを通過しなかったエントリーシグナルを 0 にクリア
        - gate_go / gate_reason 列を追加（分析用）
        """
        d = self.precompute(df)
        gate_go     = [True] * len(d)
        gate_reason = [""] * len(d)

        for i in range(len(d)):
            sig = int(d.iloc[i].get("signal", 0))
            if sig in (1, -2):
                result = self.check(d, i, sig)
                gate_go[i]     = result.go
                gate_reason[i] = result.reason
                if not result.go:
                    d.iat[i, d.columns.get_loc("signal")] = 0

        d["gate_go"]     = gate_go
        d["gate_reason"] = gate_reason

        # 一時計算列を削除
        for col in ["_sma_fast", "_sma_slow", "_vol_ma"]:
            if col in d.columns:
                d = d.drop(columns=[col])

        return d
