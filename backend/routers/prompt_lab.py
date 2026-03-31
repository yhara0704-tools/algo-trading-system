"""Prompt Lab REST API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.prompt_lab.agents import AGENT_REGISTRY, AgentRole
from backend.prompt_lab.optimizer import PromptOptimizer

router = APIRouter(prefix="/api/prompt-lab", tags=["prompt-lab"])

_optimizer: PromptOptimizer | None = None


def inject(optimizer: PromptOptimizer) -> None:
    global _optimizer
    _optimizer = optimizer


def _get_opt() -> PromptOptimizer:
    if _optimizer is None:
        raise HTTPException(503, "Optimizer not initialized")
    return _optimizer


@router.get("/agents")
async def list_agents():
    """全エージェントの現在状態を返す。"""
    opt   = _get_opt()
    status_map = {s.role: s for s in opt.get_status()}

    return [
        {
            "role":          role,
            "name":          agent.name,
            "model":         agent.model,
            "description":   agent.description,
            "version":       agent.version,
            "score":         agent.score,
            "pass_criteria": agent.pass_criteria,
            "system_prompt": agent.system_prompt,
            "is_running":    opt.is_running(role),
            "last_run_at":   status_map[role].last_run_at   if role in status_map else None,
            "last_comment":  status_map[role].last_comment  if role in status_map else "",
            "promoted":      status_map[role].promoted       if role in status_map else False,
        }
        for role, agent in AGENT_REGISTRY.items()
    ]


@router.post("/run")
async def run_all():
    """全エージェントの最適化を開始（バックグラウンド）。"""
    import asyncio
    opt = _get_opt()
    asyncio.create_task(opt.run_all())
    return {"status": "started", "agents": list(AGENT_REGISTRY.keys())}


@router.post("/run/{role}")
async def run_agent(role: AgentRole):
    """特定エージェントの最適化を開始（バックグラウンド）。"""
    import asyncio
    if role not in AGENT_REGISTRY:
        raise HTTPException(404, f"Unknown role: {role}")
    opt = _get_opt()
    asyncio.create_task(opt.run_agent(role))
    return {"status": "started", "role": role}


@router.get("/history")
async def get_history(role: AgentRole | None = None):
    """最適化履歴を返す。"""
    return _get_opt().get_history(role)


@router.get("/status")
async def get_status():
    """各エージェントの最新状態サマリー。"""
    opt = _get_opt()
    statuses = opt.get_status()
    return [
        {
            "role":          s.role,
            "name":          s.name,
            "current_score": s.current_score,
            "version":       s.version,
            "last_run_at":   s.last_run_at,
            "promoted":      s.promoted,
            "last_comment":  s.last_comment,
            "is_running":    opt.is_running(s.role),
        }
        for s in statuses
    ]
