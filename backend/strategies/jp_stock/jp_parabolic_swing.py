"""JP 株 — マルチタイムフレーム パラボリック × RCI スイング戦略.

条件（提案より）:
- 日足 PSAR 上昇トレンド（ドット < ローソク足）
- 15 分足 PSAR 上昇トレンド
- 日足 RCI(10) が上向き（直近 1 本以上で上昇）

エントリー:
- 日足 PSAR ドット価格の +5% 以上に株価到達 → +1% に指値 → 約定なら採用
- 約定しなければ見送り

損切り（``sl_mode`` で切替、既定 ``entry_pct``）:
- ``entry_pct``: エントリ価格 × (1 − ``sl_pct_from_entry``)（既定 -3%）。
  「リスク約 1〜3%」を担保するための固定 SL。100 株 × 株価 3,000 円 で約 9,000 円。
- ``psar_d``: 当該 15m 足時点の日足 PSAR。trailing するが距離が大きくなりがち。
- ``psar_15m``: 15分足 PSAR。trailing 性能は最強だが反転に過敏。

利確（``exit_logic`` で AND/OR を切替、既定 ``or``）:
- 1 時間足 RCI(10/12/15) のうち ``rci_exit_min_agree`` 本以上が ≥ ``rci_exit_threshold``
  （既定: 1 本以上 ≥ 95）
- 1 時間足 close / 1 時間足 SMA(5) − 1 ≥ ``ma_exit_dev_pct``（既定 +5%）
- ``and`` でも ``or`` でも書ける。OR は頻度確保用の保険。

時間ベース exit:
- ``max_hold_bars`` を超えて保有が続いたら次足始値で強制クローズ
  （15m × 22本/日 × 20 日 = 440 が既定）。シミュレータ側で実装する慣習。

エントリ重複抑止:
- ``entry_cooldown_bars`` 本以内に直近 entry が出ていれば signal=1 を立てない
  （既定 22 = 1 営業日）。これにより同銘柄の連投が抑制される。

エントリ直後の即時 exit 防止:
- ``min_hold_bars`` 本以内では exit シグナル (signal=-1) を抑止する
  （既定 22 = 1 営業日）。これは「1H RCI が既に高水準のままエントリ →
  同日中に exit シグナル発火 → 最悪タイミングの撤退」を防ぐ。

天井買いガード:
- ``entry_rci_h1_max`` を超える 1H RCI(10) では entry しない（既定 80）。
  「PSAR から +5% overshoot 直後に +1% リトレース」は構造上ピーク近傍を
  踏みやすいため、1H RCI 既に過熱な銘柄は除外する。

実装上の規約:
- primary df は **15 分足**（engine はこの足を 1 本ずつ進める）。
- ``self.df_d``（日足）と ``self.df_h1``（1 時間足）は **caller が事前に設定** する。
  例: 戦略インスタンス生成後、``strat.df_d = ...; strat.df_h1 = ...`` の順で代入。
  どちらかが None の場合 ``generate_signals`` は signal=0 のみを返してフェイル
  セーフとし、戦略は不発で終わる（バックテストはエラーにならない）。
- "ドット +5% 到達 → +1% 指値" は 2 段ステートで実現する:
    1. 直近 ``arming_lookback_bars`` 本の 15 分足 high が `daily_psar * (1+overshoot_pct)`
       を超えたら "armed" になる。
    2. armed 状態の間に 15 分足 low が `daily_psar * (1+entry_offset_pct)` を
       タッチしたら、その足の close で signal=1 を立てる。
       engine は次足始値で約定し、`limit_slip_pct` 制約により乖離が大きすぎれば
       スキップ（= 約定しなかった = 見送り）になる。
- stop_loss は当該 15 分足時点の **日足 PSAR 値**（asof で参照）。trailing stop の
  役割を果たす（PSAR 自体が日々進む）。
- take_profit は engine の TP 列ではなく、`generate_signals` 内で動的に
  signal=-1 を発火させて表現する（AND 条件のため動的計算が必要）。
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta
from backend.backtesting.indicators_psar import parabolic_sar

logger = logging.getLogger(__name__)


def _rci(series: pd.Series, period: int) -> pd.Series:
    """Spearman 順位相関ベースの RCI（jp_macd_rci の実装と同等）。値域 -100〜+100。"""
    n = period

    def _calc(window) -> float:
        if len(window) < n:
            return np.nan
        prices = np.array(window)
        time_rank = np.arange(1, n + 1)
        price_rank = n + 1 - pd.Series(prices).rank(ascending=False).values
        d2 = (time_rank - price_rank) ** 2
        return float((1 - 6 * d2.sum() / (n * (n ** 2 - 1))) * 100)

    return series.rolling(n).apply(_calc, raw=True)


def _asof_attach(
    primary_index: pd.DatetimeIndex,
    higher_df: pd.DataFrame,
    columns: list[str],
    *,
    suffix: str,
) -> pd.DataFrame:
    """primary の各タイムスタンプに対して higher_df の "直近過去" の値を持ってくる。

    look-ahead バイアスを避けるため、higher_df のバーは「その期間が完結したあと」
    にしか参照できない設計にする。具体的には higher のインデックスを「次の足の開始」
    として shift する（バーが完結 → その時点で値が確定）。
    """
    if higher_df is None or higher_df.empty:
        return pd.DataFrame(index=primary_index)
    h = higher_df.copy()
    if not isinstance(h.index, pd.DatetimeIndex):
        h.index = pd.to_datetime(h.index)
    # asof_merge 用: higher の各バーの値が「primary の future bar」から見えるように
    # 1 バー分シフトする（look-ahead 抑止）。1H なら +1h、1d なら +1d。
    if len(h.index) >= 2:
        delta = h.index[-1] - h.index[-2]
    else:
        delta = pd.Timedelta(0)
    h_shifted = h[columns].copy()
    h_shifted.index = h.index + delta
    h_shifted = h_shifted.sort_index()
    h_shifted.index.name = "ts"
    primary_sorted = pd.DatetimeIndex(primary_index).sort_values()
    primary_frame = pd.DataFrame({"ts": primary_sorted})
    higher_frame = h_shifted.reset_index()
    merged = pd.merge_asof(
        primary_frame.sort_values("ts"),
        higher_frame.sort_values("ts"),
        on="ts",
        direction="backward",
    )
    out = merged.set_index("ts")
    out.columns = [f"{c}_{suffix}" for c in columns]
    # primary_index の元の順序に並べ直す（DatetimeIndex 完全一致で reindex）
    return out.reindex(primary_index)


class JPParabolicSwing(StrategyBase):
    """日足 PSAR + 日足 RCI(10) up + 15m PSAR + 15m リトレース指値 + 1H RCI/MA 利確。

    Caller は ``df_d`` / ``df_h1`` を事前に attach する責務がある。
    """

    EXTRA_INTERVALS: tuple[str, ...] = ("1d", "1h")

    def __init__(
        self,
        symbol: str,
        name: str,
        *,
        interval: str = "15m",
        psar_af_start: float = 0.02,
        psar_af_step: float = 0.02,
        psar_af_max: float = 0.20,
        rci_periods: tuple[int, int, int] = (10, 12, 15),
        rci_entry_filter_period: int = 10,
        rci_exit_threshold: float = 95.0,
        rci_exit_min_agree: int = 1,
        ma_exit_period: int = 5,
        ma_exit_dev_pct: float = 0.05,
        exit_logic: str = "and",  # "or" or "and" — 利確 RCI と 5MA 乖離の連結
        overshoot_pct: float = 0.05,
        entry_offset_pct: float = 0.01,
        arming_lookback_bars: int = 16,
        require_15m_psar_up: bool = True,
        entry_psar_source: str = "15m",  # "d" (日足 PSAR) or "15m" (短足 PSAR)
        sl_mode: str = "entry_pct",  # "entry_pct" / "psar_d" / "psar_15m"
        sl_pct_from_entry: float = 0.03,  # entry_pct モード時の損切り幅（3%）
        max_hold_bars: int = 440,  # 15m × 22本/日 × 20日
        entry_cooldown_bars: int = 22,  # 1営業日分
        min_hold_bars: int = 22,  # 1営業日分: entry 直後の exit 抑止
        entry_rci_h1_max: float = 80.0,  # 1H RCI(短期) がこの値超のときは entry しない
    ) -> None:
        self.meta = StrategyMeta(
            id=f"jp_parabolic_swing_{symbol.replace('.', '_')}_{interval}",
            name=f"ParabolicSwing {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            description=(
                "MTF: 日足 PSAR up × 日足 RCI(10) up × 15m PSAR up × "
                "ドット +5% 到達後 +1% 指値リトレース。利確は 1H RCI ≥ 95 AND 1H 5MA 乖離 ≥ +15%"
            ),
            params={
                "psar_af_start": psar_af_start,
                "psar_af_step": psar_af_step,
                "psar_af_max": psar_af_max,
                "rci_periods": list(rci_periods),
                "rci_entry_filter_period": rci_entry_filter_period,
                "rci_exit_threshold": rci_exit_threshold,
                "rci_exit_min_agree": rci_exit_min_agree,
                "ma_exit_period": ma_exit_period,
                "ma_exit_dev_pct": ma_exit_dev_pct,
                "overshoot_pct": overshoot_pct,
                "entry_offset_pct": entry_offset_pct,
                "arming_lookback_bars": arming_lookback_bars,
                "require_15m_psar_up": require_15m_psar_up,
                "entry_psar_source": entry_psar_source,
                "exit_logic": exit_logic,
                "sl_mode": sl_mode,
                "sl_pct_from_entry": sl_pct_from_entry,
                "max_hold_bars": max_hold_bars,
                "entry_cooldown_bars": entry_cooldown_bars,
                "min_hold_bars": min_hold_bars,
                "entry_rci_h1_max": entry_rci_h1_max,
            },
        )
        self.psar_kwargs = {
            "af_start": float(psar_af_start),
            "af_step": float(psar_af_step),
            "af_max": float(psar_af_max),
        }
        self.rci_periods = tuple(int(p) for p in rci_periods)
        self.rci_entry_filter_period = int(rci_entry_filter_period)
        self.rci_exit_threshold = float(rci_exit_threshold)
        self.rci_exit_min_agree = max(1, min(int(rci_exit_min_agree), len(self.rci_periods)))
        self.ma_exit_period = max(2, int(ma_exit_period))
        self.ma_exit_dev_pct = float(ma_exit_dev_pct)
        self.overshoot_pct = float(overshoot_pct)
        self.entry_offset_pct = float(entry_offset_pct)
        self.arming_lookback_bars = max(1, int(arming_lookback_bars))
        self.require_15m_psar_up = bool(require_15m_psar_up)
        ep = str(entry_psar_source).strip().lower()
        if ep not in {"d", "15m"}:
            ep = "d"
        self.entry_psar_source = ep
        el = str(exit_logic).strip().lower()
        if el not in {"and", "or"}:
            el = "or"
        self.exit_logic = el
        sm = str(sl_mode).strip().lower()
        if sm not in {"entry_pct", "psar_d", "psar_15m"}:
            sm = "entry_pct"
        self.sl_mode = sm
        self.sl_pct_from_entry = max(0.0, float(sl_pct_from_entry))
        self.max_hold_bars = max(0, int(max_hold_bars))
        self.entry_cooldown_bars = max(0, int(entry_cooldown_bars))
        self.min_hold_bars = max(0, int(min_hold_bars))
        self.entry_rci_h1_max = float(entry_rci_h1_max)

        # MTF 用補助 df（caller が attach する）
        self.df_d: pd.DataFrame | None = None
        self.df_h1: pd.DataFrame | None = None

    def attach(self, *, df_d: pd.DataFrame | None = None, df_h1: pd.DataFrame | None = None) -> None:
        """日足 / 1H の OHLCV を取り付ける（caller が呼ぶ）。"""
        if df_d is not None:
            self.df_d = df_d
        if df_h1 is not None:
            self.df_h1 = df_h1

    def _safe_signals(self, df: pd.DataFrame, reason: str) -> pd.DataFrame:
        d = df.copy()
        d["signal"] = 0
        d["stop_loss"] = np.nan
        d["take_profit"] = np.nan
        d["_skip_reason"] = reason
        return d

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.df_d is None or self.df_d.empty:
            logger.warning("ParabolicSwing %s: df_d 未付与のため不発", self.meta.symbol)
            return self._safe_signals(df, "missing_df_d")
        if self.df_h1 is None or self.df_h1.empty:
            logger.warning("ParabolicSwing %s: df_h1 未付与のため不発", self.meta.symbol)
            return self._safe_signals(df, "missing_df_h1")
        if len(df) < self.arming_lookback_bars + 5:
            return self._safe_signals(df, "primary_df_too_short")

        d = df.copy()
        d["signal"] = 0
        d["stop_loss"] = np.nan
        d["take_profit"] = np.nan

        # ── 日足: PSAR + RCI(10) up ────────────────────────────────────────
        psar_d = parabolic_sar(self.df_d, **self.psar_kwargs)
        rci_d = _rci(self.df_d["close"], self.rci_entry_filter_period)
        df_d_meta = pd.DataFrame(index=self.df_d.index)
        df_d_meta["psar"] = psar_d["psar"]
        df_d_meta["psar_trend"] = psar_d["psar_trend"].astype(float)
        df_d_meta["rci"] = rci_d
        df_d_meta["rci_up"] = (rci_d.diff() > 0).astype(float)

        # ── 1H: RCI 3 本 + SMA(5) ─────────────────────────────────────────
        h1_meta = pd.DataFrame(index=self.df_h1.index)
        rci_cols_h1: list[str] = []
        for p in self.rci_periods:
            col = f"rci_{p}"
            h1_meta[col] = _rci(self.df_h1["close"], p)
            rci_cols_h1.append(col)
        h1_meta["sma5"] = self.df_h1["close"].rolling(self.ma_exit_period).mean()
        h1_meta["close"] = self.df_h1["close"]

        # ── 15m: PSAR ─────────────────────────────────────────────────────
        psar_15 = parabolic_sar(d[["high", "low", "close"]], **self.psar_kwargs)
        d["psar_15m"] = psar_15["psar"]
        d["psar_15m_trend"] = psar_15["psar_trend"].astype(float)

        # ── asof で 15m に attach ──────────────────────────────────────────
        d_attached_d = _asof_attach(
            d.index, df_d_meta, ["psar", "psar_trend", "rci", "rci_up"], suffix="d",
        )
        d_attached_h1 = _asof_attach(
            d.index, h1_meta, rci_cols_h1 + ["sma5", "close"], suffix="h1",
        )
        d = pd.concat([d, d_attached_d, d_attached_h1], axis=1)

        # ── 条件評価 ──────────────────────────────────────────────────────
        cond_d_psar_up = d["psar_trend_d"] == 1
        cond_d_rci_up = d["rci_up_d"] == 1
        cond_15_psar_up = (d["psar_15m_trend"] == 1) if self.require_15m_psar_up else True

        # 上昇トレンドかつ asof で日足 PSAR が ローソク足下 (= psar < low)
        # を念のためチェック（数値的に PSAR > low の場合は反転寸前なので除外）
        cond_d_psar_below = d["psar_d"] < d["low"]

        base_filter = cond_d_psar_up & cond_d_rci_up & cond_d_psar_below
        if self.require_15m_psar_up:
            base_filter = base_filter & cond_15_psar_up

        # 「ドット +5% に到達」と「ドット +1% に指値タッチ」のリファレンス PSAR は
        # entry_psar_source で切替: "d"=日足 PSAR / "15m"=15分足 PSAR。
        ref_psar = d["psar_d"] if self.entry_psar_source == "d" else d["psar_15m"]
        overshoot_level = ref_psar * (1.0 + self.overshoot_pct)
        touched_overshoot = (d["high"] >= overshoot_level).fillna(False)
        armed = (
            touched_overshoot.rolling(self.arming_lookback_bars, min_periods=1).sum() > 0
        )
        entry_level = ref_psar * (1.0 + self.entry_offset_pct)
        touched_entry_limit = (d["low"] <= entry_level) & (d["high"] >= entry_level)
        # ↑ 当足の値幅で entry_level を含むこと（= 約定可能性あり）

        # 天井買いガード: 1H RCI(短期) が entry_rci_h1_max を超えていれば entry しない
        # （shortest = rci_periods[0]）
        rci_short_h1_col = f"rci_{self.rci_periods[0]}_h1"
        if rci_short_h1_col in d.columns and self.entry_rci_h1_max < 100.0:
            not_overbought_h1 = d[rci_short_h1_col] < self.entry_rci_h1_max
        else:
            not_overbought_h1 = pd.Series(True, index=d.index)

        raw_long_entry = base_filter & armed & touched_entry_limit & not_overbought_h1

        # entry_cooldown_bars: 直近 N 本に signal=1 が出ている bar では再発火を抑止
        if self.entry_cooldown_bars > 0:
            cd = self.entry_cooldown_bars
            recent_entry = (
                raw_long_entry.astype(bool).shift(1).fillna(False)
                .rolling(cd, min_periods=1).sum() > 0
            )
            long_entry = raw_long_entry & ~recent_entry
        else:
            long_entry = raw_long_entry

        d.loc[long_entry, "signal"] = 1
        # SL: sl_mode で切替
        if self.sl_mode == "entry_pct":
            # エントリ価格 (= signal 発火 bar の close を近似) × (1 - sl_pct)
            d.loc[long_entry, "stop_loss"] = d.loc[long_entry, "close"] * (1.0 - self.sl_pct_from_entry)
        elif self.sl_mode == "psar_15m":
            d.loc[long_entry, "stop_loss"] = d.loc[long_entry, "psar_15m"]
        else:  # psar_d
            d.loc[long_entry, "stop_loss"] = d.loc[long_entry, "psar_d"]
        d.loc[long_entry, "take_profit"] = np.nan  # 動的 exit でハンドル

        # ── Exit (signal=-1): RCI高水準 と 5MA 乖離 を AND/OR で連結 ──
        rci_cols_attached = [f"rci_{p}_h1" for p in self.rci_periods]
        rci_high_count = sum(
            (d[c] >= self.rci_exit_threshold).astype(int) for c in rci_cols_attached
        )
        rci_high_ok = rci_high_count >= self.rci_exit_min_agree
        ma_dev = (d["close_h1"] / d["sma5_h1"]) - 1.0
        ma_dev_ok = ma_dev >= self.ma_exit_dev_pct

        if self.exit_logic == "and":
            exit_condition = rci_high_ok & ma_dev_ok
        else:
            exit_condition = rci_high_ok | ma_dev_ok
        # signal=1 が立った足には -1 を上書きしない（同一足エントリー&エグジット衝突回避）
        exit_mask = exit_condition & (d["signal"] == 0)
        d.loc[exit_mask, "signal"] = -1

        # ── 補助 / デバッグ列（engine では参照されないが探索用に残す）─────────
        d["_armed"] = armed
        d["_entry_level"] = entry_level
        d["_overshoot_level"] = overshoot_level
        d["_rci_high_count_h1"] = rci_high_count
        d["_ma_dev_h1"] = ma_dev

        return d
