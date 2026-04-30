"""JP Stock Micro-Scalping — VWAP 戻り型 +5円固定スキャル.

ユーザー提案 (2026-04-30): 三菱UFJ e スマート証券のデイトレ信用 (手数料 0 円)
を前提に「+5円 (= 100株なら +500円) を 1 分以内で取り、外れたら即損切り」を
1 日 20 回くらい繰り返して +10,000 円/日を目指す戦略。アルゴが優位な「即断即決」
領域。

ロジック (案 A: VWAP 戻り):
  - 1m バー、当日累積 VWAP (high+low+close)/3 重み付き出来高で計算
  - close <= VWAP - entry_dev_jpy で LONG (戻り狙い)
  - close >= VWAP + entry_dev_jpy で SHORT (戻り狙い)
  - 1 分 ATR が atr_min_jpy 未満の閑散時間帯はスキップ
  - TP = entry +/- tp_jpy (デフォルト +5 円)
  - SL = entry -/+ sl_jpy (デフォルト -5 円)
  - timeout_bars 経過で強制決済 (signal=-1 上書き、engine が次バーで close)

設計思想:
  - 既存 Scalp は `tp_pct=0.25%` 等の **率ベース**。MicroScalp は **絶対円**。
    → 株価 500 円でも 5,000 円でも +5 円固定 (低単価銘柄の方が ROI 良い)。
  - 1 分以内 timeout は engine 標準機能では未サポートのため、戦略側で
    エントリーバーから N バー後に signal=-1 を上書きして擬似実装。
  - 取引回数を稼ぐため `morning_only=False` 既定 (前場後場ザラ場全体)。
  - daily_loss_guard 連動は jp_live_runner 側で既存ロジックを継承。

コスト:
  - 三菱UFJ e スマート デイトレ信用: 約定手数料 0 円
  - 松井証券一日信用: 同様に 0 円
  - 信用買い金利: 1 日以内決済なら 0 円 (両証券とも)
  - 実質的なコスト = スプレッド + スリッページ。1 円刻み呼び値の銘柄
    (株価 1000-3000 円帯) で TP/SL=5 円 → R/R 比 1:1 だが、+5 円到達率 (=WR)
    が 50% を僅かに超えれば期待値プラス。

20 取引/日 × 5 円 × 100 株 = +10,000 円/日 (信用 1 銘柄、1 日)
余力 30% を 5 銘柄並走に分割すれば、各銘柄 4 取引/日 でも合計 +10,000 円/日。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPMicroScalp(StrategyBase):
    """+5円 固定スキャル (1 分以内、VWAP 戻り型)。"""

    def __init__(
        self,
        symbol:           str,
        name:             str,
        tp_jpy:           float = 5.0,    # 利確: +5 円固定
        sl_jpy:           float = 5.0,    # 損切: -5 円固定
        entry_dev_jpy:    float = 8.0,    # VWAP からの乖離閾値 (円)
        atr_period:       int   = 10,     # 1m ATR 期間
        atr_min_jpy:      float = 3.0,    # 1m ATR < 3 円の閑散帯はエントリー禁止
        atr_max_jpy:      float = 0.0,    # 1m ATR > これは過熱と判定 (0 = 無効)
        timeout_bars:     int   = 2,      # 2 バー (=2 分) 経過で強制決済
        cooldown_bars:    int   = 5,      # 直近 N バー以内に entry した銘柄は新規禁止 (連発擬陽性防止)
        avoid_open_min:   int   = 5,      # 寄付直後 5 分間はエントリー禁止 (異常値除外)
        avoid_close_min:  int   = 30,     # 大引け前 30 分は新規エントリー禁止 (eod_close 巻き込み防止)
        morning_only:     bool  = False,  # False=ザラ場全体、True=前場のみ
        allow_short:      bool  = True,   # ショート許可 (デイトレ信用なら貸株料も 0)
        max_trades_per_day: int = 0,      # 1日 N トレードで打止め (0 = 無制限)
        # 2026-04-30 グリッドサーチで判明: 9:00-9:30 + afternoon が最適、9:30-11:30 で擬陽性化
        # 許可時間帯リスト (空 = 全許可)。例: ["09:00-09:30", "12:30-15:00"]
        allowed_time_windows: list[str] | None = None,
        # 2026-04-30 寄り付きパターン分析で判明: GD_big + 初動 up は 87.5% ショート、
        # GU_big + 初動 flat は 100% ロング等、ギャップ × 初動で方向バイアスがある
        open_bias_mode:   bool = False,   # ON にすると 9:10 までの動きで 9:10-9:30 の方向を制限
        bias_observe_min: int  = 10,      # 9:00 から N 分観察してバイアスを決定
        bias_apply_until_min: int = 30,   # バイアスを 9:00+N 分まで適用 (9:30 まで)
        interval:         str   = "1m",
    ) -> None:
        self.meta = StrategyMeta(
            id=f"jp_micro_scalp_{symbol.replace('.', '_')}_{interval}",
            name=f"MicroScalp {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            description=(
                f"+{tp_jpy:.0f}円固定スキャル (1m, VWAP戻り型) — {name}. "
                f"timeout={timeout_bars} bar, dev>={entry_dev_jpy:.0f}円, "
                f"atr_min={atr_min_jpy:.0f}円. 手数料 0 円前提."
            ),
            params={
                "tp_jpy":             tp_jpy,
                "sl_jpy":             sl_jpy,
                "entry_dev_jpy":      entry_dev_jpy,
                "atr_period":         atr_period,
                "atr_min_jpy":        atr_min_jpy,
                "atr_max_jpy":        atr_max_jpy,
                "timeout_bars":       timeout_bars,
                "cooldown_bars":      cooldown_bars,
                "avoid_open_min":     avoid_open_min,
                "avoid_close_min":    avoid_close_min,
                "morning_only":       morning_only,
                "allow_short":        allow_short,
                "max_trades_per_day": max_trades_per_day,
                "allowed_time_windows": list(allowed_time_windows) if allowed_time_windows else [],
                "open_bias_mode":      open_bias_mode,
                "bias_observe_min":    bias_observe_min,
                "bias_apply_until_min": bias_apply_until_min,
            },
        )
        self.tp_jpy             = float(tp_jpy)
        self.sl_jpy             = float(sl_jpy)
        self.entry_dev_jpy      = float(entry_dev_jpy)
        self.atr_period         = int(atr_period)
        self.atr_min_jpy        = float(atr_min_jpy)
        self.atr_max_jpy        = float(atr_max_jpy)
        self.timeout_bars       = max(1, int(timeout_bars))
        self.cooldown_bars      = max(0, int(cooldown_bars))
        self.avoid_open_min     = int(avoid_open_min)
        self.avoid_close_min    = int(avoid_close_min)
        self.morning_only       = bool(morning_only)
        self.allow_short        = bool(allow_short)
        self.max_trades_per_day = max(0, int(max_trades_per_day))
        # "HH:MM-HH:MM" を (start_min, end_min) にパースして保持
        self.allowed_time_windows: list[tuple[int, int]] = []
        for w in (allowed_time_windows or []):
            try:
                a, b = w.split("-")
                ah, am = a.split(":")
                bh, bm = b.split(":")
                self.allowed_time_windows.append(
                    (int(ah) * 60 + int(am), int(bh) * 60 + int(bm))
                )
            except Exception:
                continue
        self.open_bias_mode = bool(open_bias_mode)
        self.bias_observe_min = max(1, int(bias_observe_min))
        self.bias_apply_until_min = max(self.bias_observe_min + 1, int(bias_apply_until_min))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        if d.empty:
            d["signal"] = 0
            d["stop_loss"] = np.nan
            d["take_profit"] = np.nan
            return d

        # ── 当日リセット累積 VWAP (typical price 重み付き) ──────────────────
        # index は DatetimeIndex 前提。tz が無い場合は naive で日付切り出し。
        d["_day"] = d.index.tz_convert("Asia/Tokyo").date if d.index.tz is not None else d.index.date
        typ = (d["high"] + d["low"] + d["close"]) / 3.0
        tp_x_vol = typ * d["volume"]
        d["_cum_tpv"] = tp_x_vol.groupby(d["_day"]).cumsum()
        d["_cum_vol"] = d["volume"].groupby(d["_day"]).cumsum()
        d["vwap"] = d["_cum_tpv"] / d["_cum_vol"].replace(0, np.nan)

        # ── 1m ATR (Wilder) ─────────────────────────────────────────────
        prev_close = d["close"].shift(1)
        tr = pd.concat([
            (d["high"] - d["low"]).abs(),
            (d["high"] - prev_close).abs(),
            (d["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        d["atr_jpy"] = tr.ewm(alpha=1.0 / self.atr_period, adjust=False).mean()

        # ── 時間帯フィルタ ─────────────────────────────────────────────
        idx_jst = d.index.tz_convert("Asia/Tokyo") if d.index.tz is not None else d.index
        hh = idx_jst.hour
        mm = idx_jst.minute
        # 寄付 9:00 + avoid_open_min まではエントリー禁止
        avoid_open = (hh == 9) & (mm < self.avoid_open_min)
        # 大引け 15:30 まで → 大引け前 avoid_close_min 分は新規禁止
        # (= 15:00 以降禁止 (avoid_close_min=30 のとき))
        before_close_cut = (hh == 15) & (mm >= max(0, 30 - self.avoid_close_min))
        before_close_cut |= (hh > 15)
        # morning_only: 11:30 以降禁止
        morning_block = pd.Series(False, index=d.index)
        if self.morning_only:
            morning_block = (hh > 11) | ((hh == 11) & (mm >= 30))
        # ランチ 11:30-12:30 は値がつかない (東証ザラ場休止) ので自動スキップ
        time_ok = ~(avoid_open | before_close_cut | morning_block)

        # allowed_time_windows が指定されていれば、その合算範囲のみ許可
        if self.allowed_time_windows:
            cur_min = hh * 60 + mm
            window_mask = pd.Series(False, index=d.index)
            for s, e in self.allowed_time_windows:
                window_mask |= ((cur_min >= s) & (cur_min < e))
            time_ok &= window_mask

        # ── ATR フィルタ + VWAP 乖離 ─────────────────────────────────────
        atr_ok = d["atr_jpy"] >= self.atr_min_jpy
        if self.atr_max_jpy > 0:
            atr_ok &= d["atr_jpy"] <= self.atr_max_jpy

        long_raw = time_ok & atr_ok & ((d["vwap"] - d["close"]) >= self.entry_dev_jpy)
        if not self.allow_short:
            short_raw = pd.Series(False, index=d.index)
        else:
            short_raw = time_ok & atr_ok & ((d["close"] - d["vwap"]) >= self.entry_dev_jpy)

        # ── open_bias_mode (寄り付きパターンによる方向制限) ────────────────
        #
        # 2026-04-30 寄り付きパターン分析 (data/micro_scalp_open_patterns.json) で判明:
        #   GD_big (<-1%) × 初動 up   → 87.5% ショート勝ち (戻り高値ショート)
        #   GD_big × 初動 flat        → 57.1% ショート
        #   GU_big (>+1%) × 初動 flat → 100% ロング (トレンド継続)
        #   GU_big × 初動 up/down     → 75% ショート (寄り天)
        #   flat (±0.3%) × 初動 down  → 中立
        #
        # 仕組み: 各営業日について、9:00 から `bias_observe_min` 分の動きを観察し、
        #         (gap_pct, init_dir) で 9:00+observe_min 〜 9:00+apply_until_min の
        #         エントリー方向 ("long_only" / "short_only" / "neutral") を決める。
        #
        # 前日終値は df の前営業日最後のバー (close) を使う。当日始値は当日 9 時台の
        # 最初のバーの open。yfinance は 9:00 ジャストのバーが無い日があるので注意。
        if self.open_bias_mode:
            self._apply_open_bias(d, long_raw, short_raw, idx_jst)
            long_raw = d["_long_raw_after_bias"]
            short_raw = d["_short_raw_after_bias"]

        d["signal"] = 0
        d["stop_loss"] = np.nan
        d["take_profit"] = np.nan

        # ── cooldown / max_trades_per_day を行ループで適用 ─────────────────
        # NumPy ベクトル化だと「直前 entry の N バー」を引きずる必要があるため
        # 1m × 7 日 ≒ 2,200 行なら行ループでも瞬時 (< 50 ms)。
        sig_arr = np.zeros(len(d), dtype=np.int8)
        sl_arr = np.full(len(d), np.nan)
        tp_arr = np.full(len(d), np.nan)
        long_mask = long_raw.values
        short_mask = short_raw.values
        close_arr = d["close"].values
        days_arr = pd.Series(d["_day"]).values
        cooldown = self.cooldown_bars
        last_entry_idx = -10**9
        cur_day = None
        day_count = 0
        for i in range(len(d)):
            day_i = days_arr[i]
            if day_i != cur_day:
                cur_day = day_i
                day_count = 0
            if i - last_entry_idx <= cooldown:
                continue
            if self.max_trades_per_day > 0 and day_count >= self.max_trades_per_day:
                continue
            if long_mask[i]:
                sig_arr[i] = 1
                tp_arr[i] = close_arr[i] + self.tp_jpy
                sl_arr[i] = close_arr[i] - self.sl_jpy
                last_entry_idx = i
                day_count += 1
            elif short_mask[i]:
                sig_arr[i] = -2
                tp_arr[i] = close_arr[i] - self.tp_jpy
                sl_arr[i] = close_arr[i] + self.sl_jpy
                last_entry_idx = i
                day_count += 1
        d["signal"] = sig_arr
        d["stop_loss"] = sl_arr
        d["take_profit"] = tp_arr

        # ── timeout 擬似実装: entry の N バー後に signal=-1 (= 強制決済) ────
        # engine は prev["signal"] in (-1, -2) で long 決済する。
        # short 側は (1, -1) で決済するので、long_exit/short_exit 両用に -1 を採用。
        n = self.timeout_bars
        if n > 0:
            entry_idx = np.where((sig_arr == 1) | (sig_arr == -2))[0]
            for ei in entry_idx:
                exit_pos = ei + n + 1  # entry の +n+1 バー目で決済 (engine は prev を見るので +1)
                if exit_pos < len(d) and d["signal"].iat[exit_pos] == 0:
                    # signal=-1 (= 強制決済シグナル)。stop_loss/take_profit はそのまま。
                    d.iloc[exit_pos, d.columns.get_loc("signal")] = -1

        # クリーンアップ
        for col in ("_day", "_cum_tpv", "_cum_vol", "_long_raw_after_bias", "_short_raw_after_bias"):
            if col in d.columns:
                d.drop(columns=col, inplace=True)
        return d

    # ── 寄り付きパターン分析に基づく方向バイアス ─────────────────────────
    def _apply_open_bias(
        self,
        d: pd.DataFrame,
        long_raw: pd.Series,
        short_raw: pd.Series,
        idx_jst,
    ) -> None:
        """各営業日について寄り付きパターンから 9:10-9:30 の方向制限を掛ける.

        ロジック (data/micro_scalp_open_patterns.json で確認したパターン):
          - GD (gap_pct <= -0.3%): 全初動 でショート優位 (60-87%)
          - flat (-0.3% < gap_pct < 0.3%): 中立
          - GU_mid (0.3 <= gap_pct < 1.0%): 中立 (但し down 初動は弱ロング)
          - GU_big (gap_pct >= 1.0%): 初動 flat ならロング 100%、それ以外は寄り天ショート 75%

        観察期間中 (9:00 〜 9:00+bias_observe_min) は両側許可。
        観察後 (9:00+observe_min 〜 9:00+apply_until_min) でのみ片側制限を適用。
        9:00+apply_until_min 以降は再度両側許可 (= 通常の MicroScalp に戻る)。
        """
        n = len(d)
        long_after = long_raw.copy()
        short_after = short_raw.copy()
        if n == 0:
            d["_long_raw_after_bias"] = long_after
            d["_short_raw_after_bias"] = short_after
            return

        days = pd.Series(d["_day"]).values
        close_arr = d["close"].values
        open_arr = d["open"].values

        # 各日について「前日 close (= 前日最終バーの close) と当日 open (= 当日最初の 9 時台バーの open)」を求める
        unique_days = []
        seen = set()
        for dy in days:
            if dy not in seen:
                seen.add(dy)
                unique_days.append(dy)

        # 日ごとに index 範囲を取る
        day_ranges: dict = {}
        cur_day = None
        start_i = 0
        for i in range(n):
            dy = days[i]
            if dy != cur_day:
                if cur_day is not None:
                    day_ranges[cur_day] = (start_i, i - 1)
                cur_day = dy
                start_i = i
        day_ranges[cur_day] = (start_i, n - 1)

        prev_day_close: dict = {}
        prev_dy = None
        for dy in unique_days:
            if prev_dy is not None and prev_dy in day_ranges:
                _, prev_end = day_ranges[prev_dy]
                prev_day_close[dy] = float(close_arr[prev_end])
            prev_dy = dy

        # 9:00+observe_min と 9:00+apply_until_min の境界 minute (JST 内)
        observe_end_min = 9 * 60 + self.bias_observe_min
        apply_end_min = 9 * 60 + self.bias_apply_until_min

        cur_min_arr = (idx_jst.hour * 60 + idx_jst.minute).values

        for dy in unique_days:
            if dy not in prev_day_close:
                continue  # 初日は前日 close 取れないので bias 適用せず両側許可のまま
            prev_close = prev_day_close[dy]
            s, e = day_ranges[dy]
            # 当日の 9 時台最初のバーを open_price に
            day_slice_idx = list(range(s, e + 1))
            morning_idx = [i for i in day_slice_idx if idx_jst[i].hour == 9]
            if not morning_idx:
                continue
            open_price = float(open_arr[morning_idx[0]])
            gap_pct = (open_price - prev_close) / prev_close * 100

            # 9:00+observe_min バーの close (= 観察期間終了時点)
            observe_end_idx = None
            for i in morning_idx:
                if cur_min_arr[i] >= observe_end_min:
                    observe_end_idx = i
                    break
            if observe_end_idx is None:
                continue
            close_at_observe_end = float(close_arr[observe_end_idx])
            init_drift_pct = (close_at_observe_end - open_price) / open_price * 100
            if init_drift_pct <= -0.3:
                init_dir = "down"
            elif init_drift_pct < 0.3:
                init_dir = "flat"
            else:
                init_dir = "up"

            # ── バイアス決定 ──────────────────────────────
            if gap_pct <= -0.3:
                # GD 系: 全初動でショート優位 → ロング禁止
                bias = "short_only"
            elif gap_pct >= 1.0:
                # GU_big: 初動 flat ならロング、それ以外は寄り天ショート
                bias = "long_only" if init_dir == "flat" else "short_only"
            elif gap_pct >= 0.3:
                # GU_mid: down 初動なら長ロング、それ以外は中立
                bias = "long_only" if init_dir == "down" else "neutral"
            else:
                # flat: 中立
                bias = "neutral"

            # 観察期間 (~9:00+observe_min) の後で apply_end_min まで適用
            for i in day_slice_idx:
                cm = cur_min_arr[i]
                if cm < observe_end_min:
                    continue
                if cm >= apply_end_min:
                    break
                if bias == "short_only":
                    long_after.iat[i] = False
                elif bias == "long_only":
                    short_after.iat[i] = False
                # neutral は何もしない

        d["_long_raw_after_bias"] = long_after
        d["_short_raw_after_bias"] = short_after
