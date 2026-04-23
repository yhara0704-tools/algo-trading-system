"""Parabolic SAR (Welles Wilder) — ベクトル化実装.

Daily / 5m / 15m など任意の OHLC 系列に対応する純 Python + numpy 実装。
出力は元 DataFrame と同じインデックスの ``pd.Series``（`psar`）と
``pd.Series``（`psar_trend`: +1=上昇, -1=下降）。

PSAR 仕様（Wilder の元定義）:
- 初期 AF（加速ファクタ）= ``af_start``（既定 0.02）
- AF は新しい EP（Extreme Point）が出るたびに ``af_step`` 加算（既定 0.02）
- AF の上限は ``af_max``（既定 0.20）
- 上昇トレンド中の SAR_t = SAR_{t-1} + AF * (EP - SAR_{t-1})
- 下降トレンド中の SAR_t = SAR_{t-1} - AF * (SAR_{t-1} - EP)
- SAR が当足の high/low を貫いたらトレンド反転
- 反転時: 新 SAR = 前トレンドの EP、AF を ``af_start`` にリセット、
  EP は当足の high または low に置換

戦略から見た規約:
- ``psar_trend == 1``（上昇）: ローソク足の **下** にドット、SAR 値 < low
- ``psar_trend == -1``（下降）: ローソク足の **上** にドット、SAR 値 > high
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def parabolic_sar(
    df: pd.DataFrame,
    *,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.20,
) -> pd.DataFrame:
    """OHLC DataFrame から PSAR とトレンド方向を計算する。

    Args:
        df: ``high`` / ``low`` 列を持つ DataFrame（``open`` / ``close`` は不要）。
        af_start: AF の初期値（既定 0.02）。
        af_step: 新 EP ごとの AF 加算量（既定 0.02）。
        af_max: AF の上限（既定 0.20）。

    Returns:
        元 DataFrame と同じインデックスの DataFrame。列:
            psar: SAR 値（float）
            psar_trend: +1（上昇）/ -1（下降）
            psar_ep: 現トレンドの Extreme Point
            psar_af: 現在の AF
    """
    if "high" not in df.columns or "low" not in df.columns:
        raise ValueError("parabolic_sar requires 'high' and 'low' columns")

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)

    psar = np.full(n, np.nan, dtype=float)
    trend = np.zeros(n, dtype=np.int8)
    ep_arr = np.full(n, np.nan, dtype=float)
    af_arr = np.full(n, np.nan, dtype=float)

    if n < 2:
        out = pd.DataFrame(
            {"psar": psar, "psar_trend": trend, "psar_ep": ep_arr, "psar_af": af_arr},
            index=df.index,
        )
        return out

    # 初期トレンド: 1 本目→2 本目で上昇方向に倒すか下降に倒すかは流派あり。
    # ここでは「1 本目の close < 2 本目の close なら上昇開始、それ以外は下降開始」とする。
    close = df["close"].to_numpy(dtype=float) if "close" in df.columns else None
    if close is not None and close[1] >= close[0]:
        cur_trend = 1
        cur_sar = low[0]
        cur_ep = high[1]
    elif close is not None:
        cur_trend = -1
        cur_sar = high[0]
        cur_ep = low[1]
    else:
        # close が無いケース: high の連続性で判定
        if high[1] >= high[0]:
            cur_trend = 1
            cur_sar = low[0]
            cur_ep = high[1]
        else:
            cur_trend = -1
            cur_sar = high[0]
            cur_ep = low[1]
    cur_af = af_start

    psar[1] = cur_sar
    trend[1] = cur_trend
    ep_arr[1] = cur_ep
    af_arr[1] = cur_af

    for i in range(2, n):
        prev_sar = cur_sar
        prev_ep = cur_ep
        prev_af = cur_af
        prev_trend = cur_trend

        # 仮の SAR 計算
        if prev_trend == 1:
            tentative_sar = prev_sar + prev_af * (prev_ep - prev_sar)
            # 上昇トレンド中の SAR は直近 2 本の low を超えてはいけない
            tentative_sar = min(tentative_sar, low[i - 1], low[i - 2])
        else:
            tentative_sar = prev_sar - prev_af * (prev_sar - prev_ep)
            # 下降トレンド中の SAR は直近 2 本の high を下回ってはいけない
            tentative_sar = max(tentative_sar, high[i - 1], high[i - 2])

        # 反転判定
        if prev_trend == 1:
            if low[i] < tentative_sar:
                # 上昇 → 下降に反転
                cur_trend = -1
                cur_sar = prev_ep
                cur_ep = low[i]
                cur_af = af_start
            else:
                cur_trend = 1
                cur_sar = tentative_sar
                if high[i] > prev_ep:
                    cur_ep = high[i]
                    cur_af = min(prev_af + af_step, af_max)
                else:
                    cur_ep = prev_ep
                    cur_af = prev_af
        else:
            if high[i] > tentative_sar:
                # 下降 → 上昇に反転
                cur_trend = 1
                cur_sar = prev_ep
                cur_ep = high[i]
                cur_af = af_start
            else:
                cur_trend = -1
                cur_sar = tentative_sar
                if low[i] < prev_ep:
                    cur_ep = low[i]
                    cur_af = min(prev_af + af_step, af_max)
                else:
                    cur_ep = prev_ep
                    cur_af = prev_af

        psar[i] = cur_sar
        trend[i] = cur_trend
        ep_arr[i] = cur_ep
        af_arr[i] = cur_af

    return pd.DataFrame(
        {"psar": psar, "psar_trend": trend, "psar_ep": ep_arr, "psar_af": af_arr},
        index=df.index,
    )
