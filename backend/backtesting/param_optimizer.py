"""ParamOptimizer — 適応型パラメータ探索."""
from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from backend.backtesting.strategy_factory import PARAM_RANGES


def param_hash(strategy_name: str, symbol: str, params: dict) -> str:
    """パラメータセットのハッシュ（墓場の照合用）。"""
    key = json.dumps({"s": strategy_name, "sym": symbol,
                       **{k: round(v, 6) if isinstance(v, float) else v
                          for k, v in sorted(params.items())}},
                      sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def neighborhood(base_params: dict, strategy_name: str,
                 magnitude: float = 0.15, n_variants: int = 8) -> list[dict]:
    """Robustパラメータの近傍を生成する。各パラメータを±magnitude摂動。"""
    ranges = PARAM_RANGES.get(strategy_name, {})
    if not ranges:
        return []

    variants = []
    for pname, (lo, hi, dtype) in ranges.items():
        base_val = base_params.get(pname)
        if base_val is None:
            continue
        step = (hi - lo) * magnitude
        for factor in [-1, 1]:
            new_val = base_val + step * factor
            new_val = max(lo, min(hi, new_val))
            if dtype == int:
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 6)
            if new_val != base_val:
                variant = {**base_params, pname: new_val}
                variants.append(variant)

    # sl >= tp の無効組み合わせを除外
    variants = [v for v in variants
                if v.get("sl_pct", 0) < v.get("tp_pct", 1)]
    random.shuffle(variants)
    return variants[:n_variants]


def random_sample(strategy_name: str, n_samples: int = 10,
                  graveyard_hashes: set[str] | None = None,
                  symbol: str = "") -> list[dict]:
    """ランダムなパラメータセットを生成する（墓場を除外）。"""
    ranges = PARAM_RANGES.get(strategy_name, {})
    if not ranges:
        return []

    graveyard_hashes = graveyard_hashes or set()
    samples = []
    attempts = 0

    while len(samples) < n_samples and attempts < n_samples * 5:
        attempts += 1
        params: dict[str, Any] = {}
        for pname, (lo, hi, dtype) in ranges.items():
            if dtype == int:
                params[pname] = random.randint(int(lo), int(hi))
            else:
                params[pname] = round(random.uniform(lo, hi), 6)

        # sl >= tp なら無効
        if params.get("sl_pct", 0) >= params.get("tp_pct", 1):
            continue
        # macd_fast >= macd_slow なら無効
        if params.get("macd_fast", 0) >= params.get("macd_slow", 999):
            continue

        h = param_hash(strategy_name, symbol, params)
        if h in graveyard_hashes:
            continue

        samples.append(params)

    return samples


def cross_pollinate(source_params: dict, target_symbol: str,
                    strategy_name: str) -> dict:
    """銘柄Aのベストパラメータを銘柄Bに適用する。"""
    return {**source_params}


def sensitivity_variants(base_params: dict, strategy_name: str,
                         perturbation: float = 0.10) -> list[dict]:
    """感度分析用: 各パラメータを±perturbation%変化させたバリアントを生成。"""
    ranges = PARAM_RANGES.get(strategy_name, {})
    variants = []

    for pname, (lo, hi, dtype) in ranges.items():
        base_val = base_params.get(pname)
        if base_val is None:
            continue
        for factor in [1 - perturbation, 1 + perturbation]:
            new_val = base_val * factor
            new_val = max(lo, min(hi, new_val))
            if dtype == int:
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 6)
            variant = {**base_params, pname: new_val}
            if variant.get("sl_pct", 0) < variant.get("tp_pct", 1):
                variants.append(variant)

    return variants


def compute_sensitivity(base_score: float, variant_scores: list[float]) -> float:
    """パラメータ安定性スコア (0-1)。1 = 完全安定。"""
    if not variant_scores or base_score <= 0:
        return 0.0
    avg_variant = sum(variant_scores) / len(variant_scores)
    ratio = avg_variant / base_score
    return max(0.0, min(1.0, ratio))
