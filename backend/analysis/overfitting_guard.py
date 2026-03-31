"""過学習防止モジュール — Walk-Forward + パラメータ安定性 + シャッフルテスト.

使い方:
    guard = OverfittingGuard()
    report = guard.evaluate(strategy, df, is_ratio=0.6)
    if report.is_robust:
        # 採用
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Any

from backend.backtesting.engine import run_backtest
from backend.strategies.base import StrategyBase


@dataclass
class WalkForwardResult:
    is_return:    float   # In-Sample 総リターン%
    oos_return:   float   # Out-of-Sample 総リターン%
    is_sharpe:    float
    oos_sharpe:   float
    is_win_rate:  float
    oos_win_rate: float
    oos_ratio:    float   # OOS/IS シャープ比（>0.5 が健全）


@dataclass
class RobustnessResult:
    center_score:   float        # ベストパラメータのスコア
    neighbor_scores: list[float] # 周辺パラメータのスコア
    stability:      float        # neighbor平均 / center（>0.6 が健全）


@dataclass
class ShuffleTestResult:
    real_return:     float
    shuffle_mean:    float
    shuffle_p95:     float
    p_value:         float   # 小さいほど「偶然でない」（<0.1 が健全）


@dataclass
class OverfittingReport:
    strategy_id:   str
    is_robust:     bool     # 全チェック通過
    wf:            WalkForwardResult | None = None
    robustness:    RobustnessResult  | None = None
    shuffle:       ShuffleTestResult | None = None
    warnings:      list[str] = field(default_factory=list)
    score_penalty: float = 0.0   # スコアに加算するペナルティ（マイナス値）


class OverfittingGuard:
    """バックテスト結果が過学習でないかを多角的に検証する。"""

    def __init__(
        self,
        is_ratio:        float = 0.60,   # In-Sample の割合
        min_oos_return:  float = 0.0,    # OOS リターンの最低ライン
        min_oos_ratio:   float = 0.40,   # OOS/IS シャープ比の最低ライン
        min_stability:   float = 0.55,   # パラメータ安定性の最低ライン
        shuffle_n:       int   = 200,    # シャッフル回数
        shuffle_alpha:   float = 0.15,   # シャッフルテストの有意水準
    ):
        self.is_ratio       = is_ratio
        self.min_oos_return = min_oos_return
        self.min_oos_ratio  = min_oos_ratio
        self.min_stability  = min_stability
        self.shuffle_n      = shuffle_n
        self.shuffle_alpha  = shuffle_alpha

    def evaluate(
        self,
        strategy:    StrategyBase,
        df:          pd.DataFrame,
        bt_kwargs:   dict[str, Any] | None = None,
    ) -> OverfittingReport:
        """全チェックを実行してレポートを返す。"""
        bt_kwargs = bt_kwargs or {}
        report = OverfittingReport(strategy_id=strategy.meta.id, is_robust=True)
        warnings: list[str] = []
        penalty = 0.0

        # ── 1. Walk-Forward ───────────────────────────────────────────────
        wf = self._walk_forward(strategy, df, bt_kwargs)
        report.wf = wf
        if wf is not None:
            if wf.oos_return < self.min_oos_return:
                warnings.append(f"OOSリターン不足: {wf.oos_return:.2f}%")
                penalty -= 15
                report.is_robust = False
            if wf.oos_ratio < self.min_oos_ratio:
                warnings.append(f"OOS/IS比低下: {wf.oos_ratio:.2f} < {self.min_oos_ratio}")
                penalty -= 10

        # ── 2. パラメータ安定性 ────────────────────────────────────────────
        rb = self._robustness_check(strategy, df, bt_kwargs)
        report.robustness = rb
        if rb is not None and rb.stability < self.min_stability:
            warnings.append(f"パラメータ不安定: stability={rb.stability:.2f}")
            penalty -= 8
            report.is_robust = False

        # ── 3. シャッフルテスト ───────────────────────────────────────────
        sh = self._shuffle_test(strategy, df, bt_kwargs)
        report.shuffle = sh
        if sh is not None and sh.p_value > self.shuffle_alpha:
            warnings.append(f"シャッフルテスト: p={sh.p_value:.2f} > {self.shuffle_alpha}")
            penalty -= 5

        report.warnings      = warnings
        report.score_penalty = penalty
        return report

    def _walk_forward(
        self, strategy: StrategyBase, df: pd.DataFrame, kw: dict
    ) -> WalkForwardResult | None:
        try:
            split = int(len(df) * self.is_ratio)
            if split < 50 or len(df) - split < 20:
                return None
            df_is  = df.iloc[:split]
            df_oos = df.iloc[split:]

            r_is  = run_backtest(strategy, df_is,  **kw)
            r_oos = run_backtest(strategy, df_oos, **kw)

            is_sharpe  = r_is.sharpe
            oos_sharpe = r_oos.sharpe
            ratio = (oos_sharpe / is_sharpe) if is_sharpe > 0 else 0.0

            return WalkForwardResult(
                is_return   = r_is.total_return_pct,
                oos_return  = r_oos.total_return_pct,
                is_sharpe   = is_sharpe,
                oos_sharpe  = oos_sharpe,
                is_win_rate = r_is.win_rate,
                oos_win_rate= r_oos.win_rate,
                oos_ratio   = ratio,
            )
        except Exception:
            return None

    def _robustness_check(
        self, strategy: StrategyBase, df: pd.DataFrame, kw: dict
    ) -> RobustnessResult | None:
        """EMAパラメータを±1してスコアの安定性を確認。"""
        try:
            from backend.strategies.jp_stock.jp_scalp import JPScalp
            if not isinstance(strategy, JPScalp):
                return None

            r_center = run_backtest(strategy, df, **kw)
            center   = r_center.score

            ef = strategy.ema_fast
            es = strategy.ema_slow
            neighbors = []
            for dfe, des in [(-1,0),(1,0),(0,-1),(0,1)]:
                nf, ns = ef + dfe, es + des
                if nf < 2 or ns <= nf:
                    continue
                try:
                    import copy
                    s2 = copy.deepcopy(strategy)
                    s2.ema_fast = nf
                    s2.ema_slow = ns
                    r2 = run_backtest(s2, df, **kw)
                    neighbors.append(r2.score)
                except Exception:
                    pass

            if not neighbors:
                return None
            stability = np.mean(neighbors) / center if center > 0 else 0.0
            return RobustnessResult(center, neighbors, stability)
        except Exception:
            return None

    def _shuffle_test(
        self, strategy: StrategyBase, df: pd.DataFrame, kw: dict
    ) -> ShuffleTestResult | None:
        """取引リターンをシャッフルして偶然性を検証する。"""
        try:
            result = run_backtest(strategy, df, **kw)
            if not result.trades:
                return None
            real_ret = result.total_return_pct
            pnl_pcts = np.array([t.pnl_pct for t in result.trades])

            shuffle_rets = []
            rng = np.random.default_rng(42)
            for _ in range(self.shuffle_n):
                shuffled = rng.permutation(pnl_pcts)
                eq = 1.0
                for r in shuffled:
                    eq *= (1 + r / 100)
                shuffle_rets.append((eq - 1) * 100)

            shuffle_arr = np.array(shuffle_rets)
            p_value = (shuffle_arr >= real_ret).mean()
            return ShuffleTestResult(
                real_return  = real_ret,
                shuffle_mean = float(shuffle_arr.mean()),
                shuffle_p95  = float(np.percentile(shuffle_arr, 95)),
                p_value      = float(p_value),
            )
        except Exception:
            return None
