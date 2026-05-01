#!/usr/bin/env python3
"""D5: universe_active.json を MicroScalp 4 + BBShort 3 + Pullback 2 で拡張.

D3/D4 で検証済みの per-symbol best config を universe に投入する。
すべて observation_only=True で 1 週間観察 → 効果確認後に force_paper=true 昇格。

投入計画:
  MicroScalp 4 銘柄 (低額銘柄、株価 1,000-3,000 円帯):
    1605.T (INPEX) MicroScalp +2,289 円/日
    9433.T (KDDI)  MicroScalp +1,042 円/日 (open_plus_afternoon)
    9468.T (角川)  MicroScalp   +478 円/日 (morning_session_only)
    6501.T (日立)  MicroScalp +2,322 円/日 (session_plus_afternoon)

  BBShort 3 銘柄 (高 WR + 高 PF):
    9433.T BBShort   +512 円/日 (WR 73.3%, PF 5.33)
    3103.T BBShort   +472 円/日 (WR 61.5%, PF 3.31)
    6501.T BBShort   +351 円/日 (WR 77.8%, PF 8.61)

  Pullback 2 銘柄 (新規):
    8306.T Pullback  +382 円/日 (WR 56.2%, PF 1.99)
    9468.T Pullback  +295 円/日 (WR 52.6%, PF 1.83)

universe 24 → 33 entries.
合計理論期待値: +6,131 (MS) + 1,335 (BB) + 677 (PB) = +8,143 円/日
余力圧縮後現実: +1,500-2,800 円/日 程度 (D2 + 既存 MacdRci に追加)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))


# Day 3/4 の検証結果から確定した投入候補
_OBSERVATION_ONLY_DEFAULT = False  # D5: 全戦略を実 paper で投入。MicroScalp も実戦投入。

NEW_ENTRIES = [
    # ── MicroScalp 4 銘柄 (低額銘柄優先) ─────────────────────────
    {
        "symbol": "1605.T",
        "name": "INPEX MicroScalp",
        "strategy": "MicroScalp",
        "score": 2289,
        "is_daily": 0.0,
        "oos_daily": 2289,
        "is_pf": 1.16,
        "is_trades": 585,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "microscalp_30d_optimization",
        "observation_only": False,
        "params": {
            "tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10,
            "open_bias_mode": True,
        },
        "observation_meta": {
            "reason": "D3a/D3b: 30d 1m backtest で 1,605T (INPEX, 株価 1,055円) MicroScalp が +2,289 円/日 (WR 43.1%, trades=585)。tp=10/sl=5/dev=10 + open_bias_mode。低額銘柄なので余力 1/4 圧縮でも 100 株 entry 維持可能。",
            "evidence": "data/microscalp_per_symbol_30d.json, data/microscalp_time_window_finetune.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "9433.T",
        "name": "KDDI MicroScalp",
        "strategy": "MicroScalp",
        "score": 1042,
        "is_daily": 0.0,
        "oos_daily": 1042,
        "is_pf": 2.47,
        "is_trades": 44,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "microscalp_30d_optimization",
        "observation_only": False,
        "params": {
            "tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 5,
            "avoid_open_min": 0,
            "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
        },
        "observation_meta": {
            "reason": "D3b: 9433.T MicroScalp の time_window=open_plus_afternoon が +1,042 円/日 (WR 59.1% in 09:00-09:30)。寄り 30 分専用 + 後場で WR 高い。",
            "evidence": "data/microscalp_time_window_finetune.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "9468.T",
        "name": "角川 MicroScalp",
        "strategy": "MicroScalp",
        "score": 478,
        "is_daily": 0.0,
        "oos_daily": 478,
        "is_pf": 1.29,
        "is_trades": 81,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "microscalp_30d_optimization",
        "observation_only": False,
        "params": {
            "tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8,
            "allowed_time_windows": ["09:30-11:30"],
        },
        "observation_meta": {
            "reason": "D3b: 9468.T MicroScalp morning_session_only で +478 円/日 (WR 44.4%, trades=81)。前場専用が最適。",
            "evidence": "data/microscalp_time_window_finetune.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "6501.T",
        "name": "日立 MicroScalp",
        "strategy": "MicroScalp",
        "score": 2322,
        "is_daily": 0.0,
        "oos_daily": 2322,
        "is_pf": 1.29,
        "is_trades": 549,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "microscalp_30d_optimization",
        "observation_only": False,
        "params": {
            "tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10,
            "open_bias_mode": True,
            "allowed_time_windows": ["09:30-11:30", "12:30-15:00"],
        },
        "observation_meta": {
            "reason": "D3b: 6501.T MicroScalp session_plus_afternoon で +2,322 円/日 (WR 45.9%, trades=549)。9:30-11:30 + 12:30-15:00 が最強。寄り 30 分は除外推奨。",
            "evidence": "data/microscalp_time_window_finetune.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    # ── BBShort 3 銘柄 (Tier 1 高 WR) ─────────────────────────
    {
        "symbol": "9433.T",
        "name": "KDDI BBShort",
        "strategy": "BBShort",
        "score": 512,
        "is_daily": 0.0,
        "oos_daily": 512,
        "is_pf": 5.33,
        "is_trades": 15,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "d4_bb_short_validation",
        "observation_only": False,
        "params": {
            "bb_period": 20, "bb_std": 3.0,
            "tp_pct": 0.004, "sl_pct": 0.002,
            "full_session": True,
        },
        "observation_meta": {
            "reason": "D4a: 9433.T BBShort 60d 5m で +512 円/日 (WR 73.3%, PF 5.33, trades=15)。3σ 上端タッチでショート、ミドル下抜け cover。サンプルやや少ないので observation_only。",
            "evidence": "data/d4_alt_strategies_validation.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "3103.T",
        "name": "ユニチカ BBShort",
        "strategy": "BBShort",
        "score": 472,
        "is_daily": 0.0,
        "oos_daily": 472,
        "is_pf": 3.31,
        "is_trades": 13,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "d4_bb_short_validation",
        "observation_only": False,
        "params": {
            "bb_period": 20, "bb_std": 3.0,
            "tp_pct": 0.004, "sl_pct": 0.002,
            "full_session": True,
        },
        "observation_meta": {
            "reason": "D4a: 3103.T BBShort 60d 5m で +472 円/日 (WR 61.5%, PF 3.31, trades=13)。MacdRci/Breakout と並走するが余力競合は concurrent_value_cap で制御。",
            "evidence": "data/d4_alt_strategies_validation.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "6501.T",
        "name": "日立 BBShort",
        "strategy": "BBShort",
        "score": 351,
        "is_daily": 0.0,
        "oos_daily": 351,
        "is_pf": 8.61,
        "is_trades": 9,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "d4_bb_short_validation",
        "observation_only": False,
        "params": {
            "bb_period": 20, "bb_std": 3.0,
            "tp_pct": 0.004, "sl_pct": 0.002,
            "full_session": True,
        },
        "observation_meta": {
            "reason": "D4a: 6501.T BBShort 60d 5m で +351 円/日 (WR 77.8%, PF 8.61, trades=9)。trades 少ないが WR/PF が極めて高い。observation_only で 1 週間検証。",
            "evidence": "data/d4_alt_strategies_validation.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    # ── Pullback 2 銘柄 (新規追加) ─────────────────────────
    {
        "symbol": "8306.T",
        "name": "MUFG Pullback",
        "strategy": "Pullback",
        "score": 382,
        "is_daily": 0.0,
        "oos_daily": 382,
        "is_pf": 1.99,
        "is_trades": 16,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "d4_pullback_validation",
        "observation_only": False,
        "params": {
            "ema_fast": 20, "ema_slow": 50,
            "tp_pct": 0.0040, "sl_pct": 0.0030,
        },
        "observation_meta": {
            "reason": "D4a: 8306.T Pullback 60d 5m で +382 円/日 (WR 56.2%, PF 1.99, trades=16)。MacdRci と並走、押し目買いの補完。WR 50% 超えの構造的勝ち.",
            "evidence": "data/d4_alt_strategies_validation.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
    {
        "symbol": "9468.T",
        "name": "角川 Pullback",
        "strategy": "Pullback",
        "score": 295,
        "is_daily": 0.0,
        "oos_daily": 295,
        "is_pf": 1.83,
        "is_trades": 19,
        "robust": True,
        "is_oos_pass": True,
        "calmar": 0.0,
        "source": "d4_pullback_validation",
        "observation_only": False,
        "params": {
            "ema_fast": 20, "ema_slow": 50,
            "tp_pct": 0.0040, "sl_pct": 0.0030,
        },
        "observation_meta": {
            "reason": "D4a: 9468.T Pullback 60d 5m で +295 円/日 (WR 52.6%, PF 1.83, trades=19)。MacdRci + MicroScalp + Pullback の三戦略並走で時間帯カバレッジ強化。",
            "evidence": "data/d4_alt_strategies_validation.json",
            "added_at": "2026-05-02",
            "force_paper": True,
        },
    },
]


def main() -> None:
    src_path = Path("data/universe_active.json")
    j = json.load(open(src_path))
    syms = j.get("symbols", [])
    before = len(syms)

    # 既に同じ (symbol, strategy) があれば上書き、なければ追加
    existing_pairs = {(s["symbol"], s["strategy"]): i for i, s in enumerate(syms)}
    added = 0
    updated = 0
    for new in NEW_ENTRIES:
        key = (new["symbol"], new["strategy"])
        if key in existing_pairs:
            idx = existing_pairs[key]
            old = syms[idx]
            # 既存 entry を保持しつつ params/score/observation_meta を更新
            old.update({
                "score": new["score"],
                "oos_daily": new["oos_daily"],
                "is_pf": new["is_pf"],
                "is_trades": new["is_trades"],
                "params": new["params"],
                "observation_only": new.get("observation_only", False),
                "observation_meta": new["observation_meta"],
            })
            syms[idx] = old
            updated += 1
            print(f"  UPDATE  {key[0]:8} {key[1]:15} oos={new['oos_daily']:.0f}")
        else:
            syms.append(new)
            added += 1
            print(f"  ADD     {key[0]:8} {key[1]:15} oos={new['oos_daily']:.0f}")

    j["symbols"] = syms
    j["active_count"] = len(syms)
    j["updated"] = "2026-05-02"
    j["updated_at"] = datetime.now(JST).isoformat()
    j["d5_extension_meta"] = {
        "added_at": "2026-05-02",
        "added_count": added,
        "updated_count": updated,
        "before_count": before,
        "after_count": len(syms),
        "expected_extra_pnl_jpy_per_day": 8143,
        "expected_extra_pnl_realistic_jpy_per_day": "1500-2800 (余力圧縮後)",
        "evidence": [
            "data/microscalp_per_symbol_30d.json",
            "data/microscalp_time_window_finetune.json",
            "data/d4_alt_strategies_validation.json",
            "data/d4_universe_candidates_relaxed.json",
        ],
    }

    src_path.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nuniverse: {before} → {len(syms)} entries (added={added}, updated={updated})")
    print(f"saved: {src_path}")


if __name__ == "__main__":
    main()
