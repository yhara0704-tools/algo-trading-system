"""時間帯別癖（パターン）分析 — 銘柄ごとの時間帯ボラ・勝率・傾向を蓄積。

使い方:
    store = TimePatternStore()
    store.record(symbol="7203.T", hour=9, minute=0, atr_pct=1.2, direction=1)
    report = store.get_report("7203.T")
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Literal

Direction = Literal[1, -1, 0]   # 1=上昇, -1=下降, 0=不明

_STORE_DIR = Path(__file__).parent.parent.parent / "data" / "time_patterns"
_STORE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SlotStats:
    """1時間スロット（30分単位）の累計統計."""
    hour:       int
    minute:     int          # 0 または 30
    n:          int   = 0    # サンプル数
    atr_sum:    float = 0.0  # ATR%合計
    up_count:   int   = 0    # 上昇ローソク数
    down_count: int   = 0    # 下降ローソク数
    # 高ボラ（ATR > 0.5%）イベント数
    high_vol_count: int = 0

    @property
    def avg_atr_pct(self) -> float:
        return self.atr_sum / self.n if self.n else 0.0

    @property
    def up_rate(self) -> float:
        total = self.up_count + self.down_count
        return self.up_count / total if total else 0.5

    @property
    def high_vol_rate(self) -> float:
        return self.high_vol_count / self.n if self.n else 0.0

    def to_dict(self) -> dict:
        return {
            "hour": self.hour, "minute": self.minute,
            "n": self.n,
            "avg_atr_pct": round(self.avg_atr_pct, 4),
            "up_rate": round(self.up_rate, 3),
            "high_vol_rate": round(self.high_vol_rate, 3),
        }


class TimePatternStore:
    """全銘柄の時間帯パターンを保持・永続化する。"""

    def __init__(self) -> None:
        # {symbol: {slot_key: SlotStats}}
        self._data: dict[str, dict[str, SlotStats]] = defaultdict(dict)
        self._load_all()

    # ── 記録 API ─────────────────────────────────────────────────────────────

    def record(
        self,
        symbol:   str,
        hour:     int,
        minute:   int,
        atr_pct:  float,
        direction: Direction = 0,
    ) -> None:
        """1本のローソク足データを記録する。"""
        slot_min = 0 if minute < 30 else 30
        key = f"{hour:02d}:{slot_min:02d}"
        sym_data = self._data[symbol]
        if key not in sym_data:
            sym_data[key] = SlotStats(hour=hour, minute=slot_min)
        s = sym_data[key]
        s.n          += 1
        s.atr_sum    += atr_pct
        if direction == 1:
            s.up_count += 1
        elif direction == -1:
            s.down_count += 1
        if atr_pct > 0.5:
            s.high_vol_count += 1

    def record_from_df(self, symbol: str, df) -> None:
        """pandas DataFrameから一括記録（index=datetime, columns=[open,high,low,close]）."""
        import pandas as pd
        for ts, row in df.iterrows():
            if isinstance(ts, (int, float)):
                dt = pd.Timestamp(ts, unit="ms", tz="Asia/Tokyo")
            else:
                dt = pd.Timestamp(ts)
                if dt.tzinfo is None:
                    dt = dt.tz_localize("Asia/Tokyo")
                else:
                    dt = dt.tz_convert("Asia/Tokyo")
            if row.get("high") and row.get("low") and row.get("close"):
                atr_pct  = (row["high"] - row["low"]) / row["close"] * 100
                direction = 1 if row["close"] >= row["open"] else -1
                self.record(symbol, dt.hour, dt.minute, atr_pct, direction)

    # ── 取得 API ─────────────────────────────────────────────────────────────

    def get_report(self, symbol: str) -> list[dict]:
        """時系列スロット統計を返す（ソート済み）。"""
        sym_data = self._data.get(symbol, {})
        slots = sorted(sym_data.values(), key=lambda s: (s.hour, s.minute))
        return [s.to_dict() for s in slots]

    def get_all_symbols(self) -> list[str]:
        return list(self._data.keys())

    def get_danger_zones(self, symbol: str, min_samples: int = 10) -> dict:
        """高ボラ・方向性なし時間帯（避けるべき帯）を返す。"""
        report = self.get_report(symbol)
        danger, high_vol, trend_up, trend_down = [], [], [], []
        for s in report:
            if s["n"] < min_samples:
                continue
            label = f"{s['hour']:02d}:{s['minute']:02d}"
            # 高ボラ帯（ATR > 0.3% かつ high_vol_rate > 0.4）
            if s["avg_atr_pct"] > 0.3 and s["high_vol_rate"] > 0.4:
                high_vol.append(label)
            # 方向性なし（up_rate 0.4〜0.6）
            if 0.4 <= s["up_rate"] <= 0.6:
                danger.append(label)
            # 上昇バイアス帯
            if s["up_rate"] > 0.6 and s["n"] >= min_samples:
                trend_up.append(label)
            # 下降バイアス帯
            if s["up_rate"] < 0.4 and s["n"] >= min_samples:
                trend_down.append(label)
        return {
            "high_vol_slots":  high_vol,
            "no_trend_slots":  danger,
            "bullish_slots":   trend_up,
            "bearish_slots":   trend_down,
        }

    # ── 永続化 ───────────────────────────────────────────────────────────────

    def save(self, symbol: str) -> None:
        path = _STORE_DIR / f"{symbol.replace('/', '_').replace('.', '_')}.json"
        sym_data = self._data.get(symbol, {})
        raw = {k: asdict(v) for k, v in sym_data.items()}
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))

    def save_all(self) -> None:
        for symbol in self._data:
            self.save(symbol)

    def _load_all(self) -> None:
        for path in _STORE_DIR.glob("*.json"):
            symbol = path.stem.replace("_T", ".T").replace("_USD", "-USD")
            try:
                raw = json.loads(path.read_text())
                self._data[symbol] = {
                    k: SlotStats(**v) for k, v in raw.items()
                }
            except Exception:
                pass


# シングルトン
_store: TimePatternStore | None = None

def get_store() -> TimePatternStore:
    global _store
    if _store is None:
        _store = TimePatternStore()
    return _store
