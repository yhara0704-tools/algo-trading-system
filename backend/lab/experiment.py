"""仮説検証型実験フレームワーク.

実験(Experiment):
  - 変数を1つ固定して複数グループを比較
  - グループごとにNサイクル回してから次グループへ
  - 全グループ完了後に統計比較・結論生成・Pushover通知
  - 次の実験へ自動移行
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExperimentGroup:
    name: str           # "A: 33%分散"
    description: str    # 詳細説明
    overrides: dict[str, Any]  # runner/backtest へのパラメータ上書き
    results: list[dict] = field(default_factory=list)  # 各サイクルのベスト結果


@dataclass
class Experiment:
    id: str             # "EXP-001"
    name: str           # "資金配分テスト"
    hypothesis: str     # "position_pct 50%の方が33%より収益が高い"
    variable: str       # "position_pct"
    groups: list[ExperimentGroup]
    cycles_per_group: int
    evaluation_days: int  # バックテスト期間を固定する場合。0=ローテーション継続

    # 実行状態
    current_group_idx: int = 0
    current_cycle_in_group: int = 0
    completed: bool = False
    conclusion: str = ""
    winner: str = ""

    @property
    def current_group(self) -> ExperimentGroup:
        return self.groups[self.current_group_idx]

    @property
    def total_cycles(self) -> int:
        return len(self.groups) * self.cycles_per_group

    @property
    def completed_cycles(self) -> int:
        done = self.current_group_idx * self.cycles_per_group + self.current_cycle_in_group
        return done

    def record_cycle(self, best_result: dict) -> bool:
        """1サイクルの結果を記録。グループ完了したらTrueを返す。"""
        self.current_group.results.append(best_result)
        self.current_cycle_in_group += 1
        if self.current_cycle_in_group >= self.cycles_per_group:
            self.current_group_idx += 1
            self.current_cycle_in_group = 0
            if self.current_group_idx >= len(self.groups):
                self.completed = True
            return True  # グループ完了
        return False

    def get_group_stats(self, group: ExperimentGroup) -> dict:
        """グループの統計サマリーを返す。"""
        results = [r for r in group.results if r.get("daily_pnl_jpy") is not None]
        if not results:
            return {}
        pnls   = [r["daily_pnl_jpy"] for r in results]
        wrs    = [r.get("win_rate", 0) for r in results]
        dds    = [r.get("max_drawdown_pct", 0) for r in results]
        scores = [r.get("score", 0) for r in results]
        rrs    = [abs(r.get("avg_win_jpy", 0) / r.get("avg_loss_jpy", -1))
                  for r in results if r.get("avg_loss_jpy")]
        return {
            "avg_daily_pnl":  statistics.mean(pnls),
            "avg_win_rate":   statistics.mean(wrs),
            "avg_dd":         statistics.mean(dds),
            "avg_score":      statistics.mean(scores),
            "avg_rr":         statistics.mean(rrs) if rrs else 0,
            "pnl_stdev":      statistics.stdev(pnls) if len(pnls) > 1 else 0,
            "cycles":         len(results),
        }

    def generate_conclusion(self) -> str:
        """全グループの統計を比較して結論文を生成する。"""
        lines = []
        lines.append(f"═══ {self.id}: {self.name} 結論 ═══")
        lines.append(f"仮説: {self.hypothesis}")
        lines.append("")

        stats_by_group = {}
        for g in self.groups:
            stats_by_group[g.name] = self.get_group_stats(g)
            s = stats_by_group[g.name]
            if not s:
                continue
            lines.append(f"【{g.name}】{g.description}")
            lines.append(f"  日次損益: {s['avg_daily_pnl']:+,.0f}円/日 (σ={s['pnl_stdev']:.0f})")
            lines.append(f"  勝率: {s['avg_win_rate']:.1f}%  R:R: {s['avg_rr']:.2f}  DD: {s['avg_dd']:.1f}%")
            lines.append(f"  スコア: {s['avg_score']:.1f}  ({s['cycles']}サイクル)")
            lines.append("")

        # 勝者判定: avg_score が最大のグループ
        valid = {k: v for k, v in stats_by_group.items() if v}
        if valid:
            winner_name = max(valid, key=lambda k: valid[k]["avg_score"])
            winner_group = next(g for g in self.groups if g.name == winner_name)
            self.winner = winner_name
            lines.append(f"✅ 結論: 【{winner_name}】が優位")
            lines.append(f"   設定: {winner_group.overrides}")
            lines.append(f"   スコア差: {valid[winner_name]['avg_score']:.1f} vs 他グループ")

            # 仮説の採否
            if len(self.groups) == 2:
                loser_name = [k for k in valid if k != winner_name][0]
                score_diff = valid[winner_name]["avg_score"] - valid[loser_name]["avg_score"]
                if score_diff > 2:
                    lines.append(f"   → 仮説{'支持' if self.groups[0].name == winner_name else '棄却'} (差={score_diff:.1f}pt)")
                else:
                    lines.append(f"   → 有意差なし (差={score_diff:.1f}pt) — 次の実験で再検証推奨")

        lines.append("")
        lines.append("→ 次の実験へ移行します")
        self.conclusion = "\n".join(lines)
        return self.conclusion


def build_experiment_queue() -> list[Experiment]:
    """初期実験キューを構築して返す。"""
    return [
        Experiment(
            id="EXP-001",
            name="資金配分テスト",
            hypothesis="position_pct 33%分散より50%集中の方がスコアが高い",
            variable="position_pct",
            groups=[
                ExperimentGroup(
                    name="A: 33%分散",
                    description="3銘柄同時保有前提、実質1倍レバ/ポジ",
                    overrides={"position_pct_override": 0.33},
                ),
                ExperimentGroup(
                    name="B: 50%集中",
                    description="2銘柄集中、実質1.65倍レバ/ポジ",
                    overrides={"position_pct_override": 0.50},
                ),
            ],
            cycles_per_group=10,
            evaluation_days=30,  # 期間固定して変数を純化
        ),
        Experiment(
            id="EXP-002",
            name="最適バックテスト期間",
            hypothesis="14日より30日・60日の方が安定したシグナルが出る",
            variable="days",
            groups=[
                ExperimentGroup(name="A: 14日", description="直近2週間", overrides={"days_override": 14}),
                ExperimentGroup(name="B: 30日", description="直近1ヶ月", overrides={"days_override": 30}),
                ExperimentGroup(name="C: 60日", description="直近2ヶ月", overrides={"days_override": 60}),
            ],
            cycles_per_group=8,
            evaluation_days=0,  # 0=各グループのdays_overrideを使用
        ),
        Experiment(
            id="EXP-003",
            name="寄り付き回避効果",
            hypothesis="寄り付き15分を避ける方がパフォーマンスが高い",
            variable="avoid_opening",
            groups=[
                ExperimentGroup(name="A: 寄り付き回避あり", description="09:00-09:15を除外", overrides={"avoid_opening": True}),
                ExperimentGroup(name="B: 寄り付き回避なし", description="全時間帯取引", overrides={"avoid_opening": False}),
            ],
            cycles_per_group=10,
            evaluation_days=30,
        ),
    ]


class ExperimentManager:
    """実験キューを管理し、現在の実験状態を返すクラス。"""

    def __init__(self) -> None:
        self._queue: list[Experiment] = build_experiment_queue()
        self._current_idx: int = 0
        self._completed: list[Experiment] = []

    @property
    def current(self) -> Experiment | None:
        if self._current_idx < len(self._queue):
            return self._queue[self._current_idx]
        return None

    def get_overrides(self) -> dict:
        """現在のグループのオーバーライドパラメータを返す。"""
        exp = self.current
        if not exp:
            return {}
        return exp.current_group.overrides

    def get_days_override(self) -> int | None:
        """days_overrideが設定されていれば返す。なければNone。"""
        overrides = self.get_overrides()
        return overrides.get("days_override")

    def record_cycle(self, results: list[dict]) -> dict | None:
        """
        1サイクルの結果を記録。
        グループ完了時: {'group_done': True, 'group_name': ..., 'stats': ...}
        実験完了時: {'experiment_done': True, 'conclusion': ..., 'winner': ...}
        それ以外: None
        """
        exp = self.current
        if not exp:
            return None

        # JP株のベスト結果を抽出
        jp_done = [r for r in results
                   if r.get("status") == "done"
                   and r.get("symbol", "").endswith(".T")
                   and r.get("num_trades", 0) > 0]
        if not jp_done:
            return None
        best = max(jp_done, key=lambda r: r.get("score", 0))

        group_name = exp.current_group.name
        group_complete = exp.record_cycle(best)

        if not group_complete:
            return None

        if exp.completed:
            # 実験完了
            conclusion = exp.generate_conclusion()
            self._completed.append(exp)
            self._current_idx += 1
            logger.info("Experiment %s completed. Winner: %s", exp.id, exp.winner)
            return {
                "experiment_done": True,
                "exp_id": exp.id,
                "exp_name": exp.name,
                "conclusion": conclusion,
                "winner": exp.winner,
            }
        else:
            # グループ完了、次グループへ
            stats = exp.get_group_stats(next(g for g in exp.groups if g.name == group_name))
            logger.info("Group %s done. avg_pnl=%.0f", group_name, stats.get("avg_daily_pnl", 0))
            return {
                "group_done": True,
                "group_name": group_name,
                "next_group": exp.current_group.name,
                "stats": stats,
            }

    def get_status(self) -> dict:
        """UIに表示するステータスを返す。"""
        exp = self.current
        if not exp:
            return {
                "status": "全実験完了",
                "completed": [{"id": e.id, "name": e.name, "winner": e.winner} for e in self._completed],
            }
        return {
            "status": "running",
            "exp_id": exp.id,
            "exp_name": exp.name,
            "hypothesis": exp.hypothesis,
            "variable": exp.variable,
            "current_group": exp.current_group.name,
            "current_group_desc": exp.current_group.description,
            "cycle_in_group": exp.current_cycle_in_group,
            "cycles_per_group": exp.cycles_per_group,
            "completed_cycles": exp.completed_cycles,
            "total_cycles": exp.total_cycles,
            "groups": [
                {
                    "name": g.name,
                    "cycles_done": len(g.results),
                    "stats": exp.get_group_stats(g) if g.results else {},
                }
                for g in exp.groups
            ],
            "completed_experiments": [
                {"id": e.id, "name": e.name, "winner": e.winner, "conclusion": e.conclusion}
                for e in self._completed
            ],
        }
