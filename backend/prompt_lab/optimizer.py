"""プロンプト最適化ループ — 候補生成 → 評価 → 合格なら本番昇格."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .agents import AgentDef, AgentRole, AGENT_REGISTRY, promote_prompt
from .evaluator import evaluate_prompt, EvalResult
from .models import MODEL_OPTIMIZER

logger = logging.getLogger(__name__)

# スコアがこれを超えたら本番に昇格
PROMOTE_THRESHOLD = 0.75
# 1回の最適化で生成する候補数
NUM_CANDIDATES    = 3

_OPTIMIZER_SYSTEM = """あなたはAIエージェントのプロンプトエンジニアです。
与えられた情報をもとに、より優れたsystem promptを提案してください。"""

_OPTIMIZER_USER_TPL = """## エージェント役割
{description}

## 現在のsystem prompt
{current_prompt}

## 現在のスコア: {score:.2f}

## 不合格だった基準
{failed_criteria}

## 改善されたsystem promptを1つ提案してください
- 不合格基準を重点的に改善する
- 合格した基準を維持する
- 簡潔かつ行動可能な内容を保つ
- 回答はsystem prompt本文のみ（説明文や```は不要）"""


@dataclass
class OptimizationRun:
    role:          AgentRole
    timestamp:     float
    initial_score: float
    final_score:   float
    promoted:      bool
    version:       int
    candidates:    list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LabStatus:
    """各エージェントの最新最適化状況."""
    role:          AgentRole
    name:          str
    current_score: float
    version:       int
    last_run_at:   float
    promoted:      bool
    last_comment:  str


class PromptOptimizer:
    """全エージェントのプロンプトを自動最適化する."""

    def __init__(self) -> None:
        api_key = __import__("os").getenv("ANTHROPIC_API_KEY", "")
        self._client  = anthropic.AsyncAnthropic(api_key=api_key)
        self._history: list[OptimizationRun]   = []
        self._status:  dict[AgentRole, LabStatus] = {}
        self._running: set[AgentRole]            = set()

    # ── 公開 API ─────────────────────────────────────────────────────────────

    async def run_all(self) -> list[LabStatus]:
        """全エージェントを並列最適化する."""
        tasks = [self._optimize_agent(role) for role in AGENT_REGISTRY]
        await asyncio.gather(*tasks, return_exceptions=True)
        return self.get_status()

    async def run_agent(self, role: AgentRole) -> LabStatus:
        """単一エージェントを最適化する."""
        await self._optimize_agent(role)
        return self._status.get(role)

    def get_status(self) -> list[LabStatus]:
        return list(self._status.values())

    def get_history(self, role: AgentRole | None = None) -> list[dict]:
        runs = self._history if role is None else [r for r in self._history if r.role == role]
        return [
            {
                "role":          r.role,
                "timestamp":     r.timestamp,
                "initial_score": r.initial_score,
                "final_score":   r.final_score,
                "promoted":      r.promoted,
                "version":       r.version,
                "candidates":    r.candidates,
            }
            for r in runs[-50:]  # 直近50件
        ]

    def is_running(self, role: AgentRole) -> bool:
        return role in self._running

    # ── 内部処理 ─────────────────────────────────────────────────────────────

    async def _optimize_agent(self, role: AgentRole) -> None:
        if role in self._running:
            logger.info("Optimizer already running for %s — skip", role)
            return

        agent = AGENT_REGISTRY[role]
        self._running.add(role)
        logger.info("Optimizer start: %s (v%d)", role, agent.version)

        run = OptimizationRun(
            role=role,
            timestamp=time.time(),
            initial_score=agent.score,
            final_score=agent.score,
            promoted=False,
            version=agent.version,
        )

        try:
            # Step 1: 現在プロンプトを評価
            base_score, base_results = await evaluate_prompt(agent, agent.system_prompt, self._client)
            logger.info("[%s] base score=%.2f", role, base_score)

            best_score  = base_score
            best_prompt = agent.system_prompt
            best_results = base_results

            # Step 2: 候補プロンプトを生成・評価
            for i in range(NUM_CANDIDATES):
                candidate = await self._generate_candidate(agent, base_score, base_results)
                if not candidate:
                    continue

                score, results = await evaluate_prompt(agent, candidate, self._client)
                logger.info("[%s] candidate %d score=%.2f", role, i + 1, score)

                run.candidates.append({
                    "index":   i + 1,
                    "score":   score,
                    "prompt":  candidate[:200] + "..." if len(candidate) > 200 else candidate,
                    "comment": results[-1].comment if results else "",
                })

                if score > best_score:
                    best_score   = score
                    best_prompt  = candidate
                    best_results = results

            # Step 3: 合格なら昇格
            run.final_score = best_score
            if best_score >= PROMOTE_THRESHOLD and best_score > agent.score:
                new_version = agent.version + 1
                promote_prompt(role, best_prompt, new_version, best_score)
                run.promoted = True
                run.version  = new_version
                logger.info("[%s] PROMOTED v%d score=%.2f", role, new_version, best_score)

            # ステータス更新
            last_comment = best_results[-1].comment if best_results else ""
            self._status[role] = LabStatus(
                role=role, name=agent.name,
                current_score=best_score,
                version=agent.version,
                last_run_at=time.time(),
                promoted=run.promoted,
                last_comment=last_comment,
            )

        except Exception as e:
            logger.error("Optimizer error for %s: %s", role, e)
        finally:
            self._running.discard(role)
            self._history.append(run)

    async def _generate_candidate(
        self,
        agent: AgentDef,
        score: float,
        results: list[EvalResult],
    ) -> str | None:
        """不合格基準を改善する候補プロンプトを生成する。"""
        failed_criteria = []
        for r in results:
            for idx in r.failed:
                if idx < len(agent.pass_criteria):
                    failed_criteria.append(f"- {agent.pass_criteria[idx]}")
        if not failed_criteria:
            failed_criteria = ["- すべての基準を満たしているが、さらに精度を上げる"]

        user_msg = _OPTIMIZER_USER_TPL.format(
            description=agent.description,
            current_prompt=agent.system_prompt,
            score=score,
            failed_criteria="\n".join(failed_criteria),
        )

        try:
            resp = await self._client.messages.create(
                model=MODEL_OPTIMIZER,
                max_tokens=1024,
                system=_OPTIMIZER_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            logger.error("Candidate generation failed: %s", e)
            return None
