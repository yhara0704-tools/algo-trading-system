"""戦略知識ベース — 地合い別パフォーマンス履歴・失敗理由・再利用メモを蓄積。

「この相場ではダメだったが、別の相場では使えるかも」という知見を保持する。
バックテストのたびに記録が積み上がり、地合い別の適性マップが自動生成される。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).parent.parent.parent / "data" / "strategy_knowledge.json"
_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class RunRecord:
    """1回のバックテスト実行記録。"""
    ts:              float   # Unix timestamp
    regime:          str     # 地合い (上昇トレンド/レンジ 等)
    days:            int     # バックテスト期間
    win_rate:        float
    profit_factor:   float
    daily_pnl_jpy:   float
    max_drawdown_pct: float
    num_trades:      int
    sharpe:          float
    score:           float
    # 失敗分析
    failed_criteria: list[str] = field(default_factory=list)  # 何が基準未達か
    notes:           str = ""                                   # 自動生成メモ


@dataclass
class RegimeStat:
    """地合い別の累計統計。"""
    regime:       str
    n:            int   = 0
    score_sum:    float = 0.0
    pnl_sum:      float = 0.0
    win_rate_sum: float = 0.0
    best_score:   float = 0.0
    worst_score:  float = 1.0

    @property
    def avg_score(self) -> float:
        return self.score_sum / self.n if self.n else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.pnl_sum / self.n if self.n else 0.0

    @property
    def avg_win_rate(self) -> float:
        return self.win_rate_sum / self.n if self.n else 0.0

    def verdict(self) -> str:
        if self.n < 2:
            return "データ不足"
        if self.avg_score >= 0.65:
            return "◎ 相性良好"
        if self.avg_score >= 0.45:
            return "△ 条件次第"
        return "✗ 不適合"


@dataclass
class StrategyKnowledge:
    """1戦略の全知識。"""
    strategy_id:   str
    strategy_name: str
    symbol:        str
    description:   str
    records:       list[RunRecord] = field(default_factory=list)
    # {regime: RegimeStat}
    regime_stats:  dict[str, RegimeStat] = field(default_factory=dict)
    best_regime:   str = ""
    worst_regime:  str = ""
    total_runs:    int = 0
    last_run_ts:   float = 0.0

    _MAX_RECORDS = 30   # JSONに保持する最大件数（古いものはSQLiteへ移行済み）

    def add_record(self, rec: RunRecord) -> None:
        self.records.append(rec)
        self.total_runs += 1
        self.last_run_ts = rec.ts
        # メモリ・JSONの肥大化防止: 古いレコードは切り捨てる
        if len(self.records) > self._MAX_RECORDS:
            self.records = self.records[-self._MAX_RECORDS:]

        # 地合い別統計を更新
        if rec.regime not in self.regime_stats:
            self.regime_stats[rec.regime] = RegimeStat(regime=rec.regime)
        rs = self.regime_stats[rec.regime]
        rs.n            += 1
        rs.score_sum    += rec.score
        rs.pnl_sum      += rec.daily_pnl_jpy
        rs.win_rate_sum += rec.win_rate
        rs.best_score   = max(rs.best_score, rec.score)
        rs.worst_score  = min(rs.worst_score, rec.score)

        # 最良・最悪地合いを更新
        if self.regime_stats:
            qualified = [s for s in self.regime_stats.values() if s.n >= 2]
            if qualified:
                self.best_regime  = max(qualified, key=lambda s: s.avg_score).regime
                self.worst_regime = min(qualified, key=lambda s: s.avg_score).regime

    def get_regime_summary(self) -> list[dict]:
        return sorted(
            [
                {
                    "regime":     rs.regime,
                    "n":          rs.n,
                    "avg_score":  round(rs.avg_score, 3),
                    "avg_pnl":    round(rs.avg_pnl, 0),
                    "avg_win_rate": round(rs.avg_win_rate, 1),
                    "best_score": round(rs.best_score, 3),
                    "verdict":    rs.verdict(),
                }
                for rs in self.regime_stats.values()
            ],
            key=lambda x: x["avg_score"],
            reverse=True,
        )

    def get_insights(self) -> list[str]:
        """人間が読める洞察メモを自動生成する。"""
        insights = []
        qualified = [s for s in self.regime_stats.values() if s.n >= 2]

        good  = [s for s in qualified if s.avg_score >= 0.60]
        bad   = [s for s in qualified if s.avg_score < 0.35]
        maybe = [s for s in qualified if 0.35 <= s.avg_score < 0.60]

        if good:
            names = "・".join(s.regime for s in good)
            insights.append(f"✓ {names} では安定してパフォーマンス良好")
        if bad:
            names = "・".join(s.regime for s in bad)
            insights.append(f"✗ {names} では不適合 — エントリー回避を推奨")
        if maybe:
            names = "・".join(s.regime for s in maybe)
            insights.append(f"△ {names} では条件次第 — パラメータ調整で改善余地あり")

        # 最近の失敗パターン
        recent = sorted(self.records, key=lambda r: r.ts, reverse=True)[:5]
        failed_set: dict[str, int] = defaultdict(int)
        for r in recent:
            for f in r.failed_criteria:
                failed_set[f] += 1
        if failed_set:
            top_fail = max(failed_set, key=lambda k: failed_set[k])
            insights.append(f"⚠ 直近の主な課題: 「{top_fail}」({failed_set[top_fail]}/5回)")

        if self.total_runs < 3:
            insights.append("📊 データ蓄積中 — 判断には最低3回の実行が必要")

        return insights

    def get_recent_records(self, n: int = 10) -> list[dict]:
        return [asdict(r) for r in sorted(self.records, key=lambda r: r.ts, reverse=True)[:n]]


# ── PDCA目標との照合 ─────────────────────────────────────────────────────────

_PDCA_CRITERIA = {
    "win_rate":        ("勝率不足", lambda v, t: v >= t),
    "profit_factor":   ("PF不足(1.3未満)", lambda v, _: v >= 1.3),
    "daily_pnl_jpy":   ("日次損益が目標未達", lambda v, t: v >= t),
    "max_drawdown_pct":("最大DDが大きすぎる", lambda v, t: v >= t),
    "num_trades":      ("取引数不足(統計的信頼性低)", lambda v, _: v >= 10),
}

def analyze_failure(
    result: dict,
    target_win_rate: float  = 52.0,
    target_daily_jpy: float = 1000.0,
    target_max_dd: float    = -8.0,
) -> tuple[list[str], str]:
    """バックテスト結果から失敗理由と改善メモを生成する。"""
    failed: list[str] = []

    checks = {
        "win_rate":         (result.get("win_rate", 0),        target_win_rate),
        "profit_factor":    (result.get("profit_factor", 0),   1.3),
        "daily_pnl_jpy":    (result.get("daily_pnl_jpy", 0),  target_daily_jpy),
        "max_drawdown_pct": (result.get("max_drawdown_pct", -100), target_max_dd),
        "num_trades":       (result.get("num_trades", 0),      10),
    }
    for key, (val, target) in checks.items():
        label, ok_fn = _PDCA_CRITERIA[key]
        if not ok_fn(val, target):
            failed.append(label)

    # 改善メモ自動生成
    notes_parts = []
    wr  = result.get("win_rate", 0)
    pf  = result.get("profit_factor", 0)
    pnl = result.get("daily_pnl_jpy", 0)
    dd  = result.get("max_drawdown_pct", 0)
    nt  = result.get("num_trades", 0)

    if nt < 10:
        notes_parts.append("取引数が少なく統計的信頼性が低い。エントリー条件を緩めるか期間を延ばす")
    if pf < 1.0:
        notes_parts.append("PF<1.0=損失超過。SL幅を縮小するかTP幅を拡大する")
    elif pf < 1.3:
        notes_parts.append("PFは正だが低い。エグジット条件の見直しで改善余地あり")
    if wr > 0 and wr < 45:
        notes_parts.append(f"勝率{wr:.1f}%は低い。エントリー条件をより厳しく絞ることを検討")
    if dd < -15:
        notes_parts.append(f"最大DD={dd:.1f}%は大きい。ポジションサイズ縮小またはSL引き締めを検討")
    if pnl > 0 and not failed:
        notes_parts.append("全基準クリア。さらなる最適化でスコア向上を狙う")
    if pnl < -500:
        notes_parts.append("損失が大きい。この地合いでのエントリー自体を見直す")

    return failed, " / ".join(notes_parts) if notes_parts else "特記事項なし"


# ── KnowledgeBase クラス ─────────────────────────────────────────────────────

class StrategyKnowledgeBase:
    """全戦略の知識を管理・永続化する。"""

    def __init__(self) -> None:
        self._db: dict[str, StrategyKnowledge] = {}
        self._load()

    def record(
        self,
        result: dict,
        regime: str,
        target_daily_jpy: float = 1000.0,
    ) -> None:
        """バックテスト結果を記録する。regime は現在の地合い文字列。"""
        sid  = result.get("strategy_id", "unknown")
        name = result.get("strategy_name", sid)
        sym  = result.get("symbol", "")
        desc = result.get("description", "")

        if sid not in self._db:
            self._db[sid] = StrategyKnowledge(
                strategy_id=sid, strategy_name=name,
                symbol=sym, description=desc,
            )

        failed, notes = analyze_failure(result, target_daily_jpy=target_daily_jpy)

        rec = RunRecord(
            ts               = time.time(),
            regime           = regime or "不明",
            days             = result.get("days", 0),
            win_rate         = result.get("win_rate", 0),
            profit_factor    = result.get("profit_factor", 0),
            daily_pnl_jpy    = result.get("daily_pnl_jpy", 0),
            max_drawdown_pct = result.get("max_drawdown_pct", 0),
            num_trades       = result.get("num_trades", 0),
            sharpe           = result.get("sharpe", 0),
            score            = result.get("score", 0),
            failed_criteria  = failed,
            notes            = notes,
        )
        self._db[sid].add_record(rec)

    def get_knowledge(self, strategy_id: str) -> StrategyKnowledge | None:
        return self._db.get(strategy_id)

    def get_all(self) -> list[dict]:
        return [self._serialize(k) for k in self._db.values()]

    def get_best_for_regime(self, regime: str, top_n: int = 3) -> list[dict]:
        """特定地合いで最もパフォーマンスが良い戦略を返す。"""
        candidates = []
        for k in self._db.values():
            rs = k.regime_stats.get(regime)
            if rs and rs.n >= 1:
                candidates.append((rs.avg_score, rs.avg_pnl, self._serialize(k)))
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [c[2] for c in candidates[:top_n]]

    def get_regime_map(self) -> dict[str, list[dict]]:
        """地合い→推奨戦略のマップを返す。"""
        regimes: dict[str, list] = defaultdict(list)
        for k in self._db.values():
            for regime, rs in k.regime_stats.items():
                if rs.n >= 1 and rs.avg_score > 0:
                    regimes[regime].append({
                        "strategy_id":   k.strategy_id,
                        "strategy_name": k.strategy_name,
                        "avg_score":     round(rs.avg_score, 3),
                        "avg_pnl":       round(rs.avg_pnl, 0),
                        "verdict":       rs.verdict(),
                        "n":             rs.n,
                    })
        # 各地合いでスコア順ソート
        return {r: sorted(v, key=lambda x: x["avg_score"], reverse=True)
                for r, v in regimes.items()}

    def save(self) -> None:
        raw = {sid: self._to_raw(k) for sid, k in self._db.items()}
        _STORE_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2))

    def _load(self) -> None:
        if not _STORE_PATH.exists():
            return
        try:
            raw = json.loads(_STORE_PATH.read_text())
            for sid, data in raw.items():
                k = StrategyKnowledge(
                    strategy_id=data["strategy_id"],
                    strategy_name=data["strategy_name"],
                    symbol=data.get("symbol", ""),
                    description=data.get("description", ""),
                    total_runs=data.get("total_runs", 0),
                    last_run_ts=data.get("last_run_ts", 0),
                    best_regime=data.get("best_regime", ""),
                    worst_regime=data.get("worst_regime", ""),
                )
                for r in data.get("records", []):
                    k.records.append(RunRecord(**r))
                for regime, rs_data in data.get("regime_stats", {}).items():
                    k.regime_stats[regime] = RegimeStat(**rs_data)
                self._db[sid] = k
        except Exception:
            pass

    def _to_raw(self, k: StrategyKnowledge) -> dict:
        return {
            "strategy_id":   k.strategy_id,
            "strategy_name": k.strategy_name,
            "symbol":        k.symbol,
            "description":   k.description,
            "total_runs":    k.total_runs,
            "last_run_ts":   k.last_run_ts,
            "best_regime":   k.best_regime,
            "worst_regime":  k.worst_regime,
            "records":       [asdict(r) for r in k.records[-200:]],  # 最大200件保持
            "regime_stats":  {r: asdict(s) for r, s in k.regime_stats.items()},
        }

    def _serialize(self, k: StrategyKnowledge) -> dict:
        return {
            "strategy_id":   k.strategy_id,
            "strategy_name": k.strategy_name,
            "symbol":        k.symbol,
            "total_runs":    k.total_runs,
            "last_run_ts":   k.last_run_ts,
            "best_regime":   k.best_regime,
            "worst_regime":  k.worst_regime,
            "regime_summary": k.get_regime_summary(),
            "insights":      k.get_insights(),
            "recent_records": k.get_recent_records(5),
        }


# シングルトン
_kb: StrategyKnowledgeBase | None = None

def get_kb() -> StrategyKnowledgeBase:
    global _kb
    if _kb is None:
        _kb = StrategyKnowledgeBase()
    return _kb
