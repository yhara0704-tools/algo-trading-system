"""プロンプト評価器 — テストケース定義 + Claude-as-Judgeによるスコアリング."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from .agents import AgentDef, AgentRole
from .models import MODEL_JUDGE

logger = logging.getLogger(__name__)

# ── テストケース ──────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    name:        str
    user_input:  str      # エージェントへの入力
    description: str      # 何を確認するテスト


# 各エージェントのテストケース定義
TEST_CASES: dict[AgentRole, list[TestCase]] = {

"market_analyst": [
    TestCase(
        name="trend_up",
        user_input=json.dumps({
            "symbol": "BTC-USD", "price": 95000,
            "adx": 32, "atr_ratio": 0.012,
            "ema9": 94500, "ema21": 93800, "ema200": 88000,
            "rsi": 62,
        }, ensure_ascii=False),
        description="上昇トレンド局面の分析",
    ),
    TestCase(
        name="range_low_vol",
        user_input=json.dumps({
            "symbol": "BTC-USD", "price": 91000,
            "adx": 14, "atr_ratio": 0.004,
            "ema9": 91200, "ema21": 91100, "ema200": 89000,
            "rsi": 48,
        }, ensure_ascii=False),
        description="レンジ・低ボラ局面の分析",
    ),
    TestCase(
        name="high_vol_spike",
        user_input=json.dumps({
            "symbol": "BTC-USD", "price": 88000,
            "adx": 45, "atr_ratio": 0.025,
            "ema9": 89000, "ema21": 90500, "ema200": 88500,
            "rsi": 28,
        }, ensure_ascii=False),
        description="高ボラ・急落局面の分析",
    ),
],

"strategy_selector": [
    TestCase(
        name="uptrend_with_data",
        user_input=json.dumps({
            "regime": "上昇トレンド",
            "strategies": [
                {"name": "BTCEmaCross",   "win_rate": 0.56, "daily_pnl_jpy": 1800, "score": 0.72},
                {"name": "BTCRsiBollinger", "win_rate": 0.48, "daily_pnl_jpy": 900, "score": 0.51},
                {"name": "BTCVwapReversion","win_rate": 0.52, "daily_pnl_jpy": 1200, "score": 0.60},
            ]
        }, ensure_ascii=False),
        description="上昇トレンドで最適戦略を1つ選択する",
    ),
    TestCase(
        name="range_with_data",
        user_input=json.dumps({
            "regime": "レンジ",
            "strategies": [
                {"name": "BTCEmaCross",     "win_rate": 0.42, "daily_pnl_jpy": -200, "score": 0.30},
                {"name": "BTCVwapReversion","win_rate": 0.58, "daily_pnl_jpy": 1500, "score": 0.75},
            ]
        }, ensure_ascii=False),
        description="レンジ相場で最適戦略を1つ選択する",
    ),
],

"risk_assessor": [
    TestCase(
        name="safe_trade",
        user_input=json.dumps({
            "symbol": "BTC-USD", "side": "BUY",
            "entry_price": 94000, "quantity": 0.01,
            "account_balance": 100000,
            "regime": "上昇トレンド",
        }, ensure_ascii=False),
        description="低リスクトレードのGO判定",
    ),
    TestCase(
        name="risky_trade",
        user_input=json.dumps({
            "symbol": "BTC-USD", "side": "BUY",
            "entry_price": 94000, "quantity": 1.0,
            "account_balance": 100000,
            "regime": "高ボラ",
        }, ensure_ascii=False),
        description="高リスクトレードのNO-GO判定",
    ),
],

"pdca_advisor": [
    TestCase(
        name="poor_performance",
        user_input=json.dumps({
            "goal": "日次1000円安定",
            "results": [
                {"name": "BTCEmaCross",    "win_rate": 0.44, "pf": 0.92, "daily_pnl_jpy": -300, "max_dd": 0.05},
                {"name": "BTCVwapReversion","win_rate": 0.51, "pf": 1.05, "daily_pnl_jpy": 200, "max_dd": 0.03},
            ],
            "current_stage": 1, "target_daily_jpy": 1000,
        }, ensure_ascii=False),
        description="低パフォーマンスへの改善提案",
    ),
    TestCase(
        name="near_goal",
        user_input=json.dumps({
            "goal": "日次1000円安定",
            "results": [
                {"name": "BTCEmaCross",    "win_rate": 0.55, "pf": 1.35, "daily_pnl_jpy": 850, "max_dd": 0.02},
                {"name": "BTCVwapReversion","win_rate": 0.58, "pf": 1.42, "daily_pnl_jpy": 1100, "max_dd": 0.025},
            ],
            "current_stage": 1, "target_daily_jpy": 1000,
        }, ensure_ascii=False),
        description="目標近接時のさらなる改善提案",
    ),
],

# ── JP株専用エージェント テストケース ─────────────────────────────────────────

"jp_market_analyst": [
    TestCase(
        name="bullish_morning_open",
        user_input=json.dumps({
            "session": "前場寄り付き前",
            "nikkei_futures_premium": 150,
            "usdjpy": 149.8,
            "prev_day_nikkei_change_pct": 1.2,
            "prev_day_topix_change_pct": 0.9,
            "pts_active_sectors": ["半導体", "銀行"],
            "vix": 18.5,
        }, ensure_ascii=False),
        description="先物プレミアム・円安・強気環境での前場方針",
    ),
    TestCase(
        name="bearish_afternoon",
        user_input=json.dumps({
            "session": "後場",
            "nikkei_futures_premium": -200,
            "usdjpy": 147.2,
            "prev_day_nikkei_change_pct": -1.8,
            "current_nikkei_change_pct": -0.5,
            "high_vol_sectors": ["エネルギー", "防衛"],
            "vix": 24.0,
        }, ensure_ascii=False),
        description="後場弱気環境での方針転換",
    ),
    TestCase(
        name="event_risk_day",
        user_input=json.dumps({
            "session": "前場",
            "nikkei_futures_premium": 30,
            "usdjpy": 150.1,
            "events_today": ["日銀金融政策決定会合", "米CPI発表(夜)"],
            "prev_day_nikkei_change_pct": 0.1,
            "vix": 21.0,
        }, ensure_ascii=False),
        description="イベントリスク日の慎重方針",
    ),
],

"jp_strategy_selector": [
    TestCase(
        name="orb_morning",
        user_input=json.dumps({
            "regime": "強気",
            "session_time": "09:05",
            "candidates": [
                {"symbol": "9984.T", "name": "SoftBank", "orb_score": 0.82, "vwap_score": 0.45},
                {"symbol": "6758.T", "name": "Sony",     "orb_score": 0.71, "vwap_score": 0.60},
            ],
            "backtest_results": [
                {"strategy": "jp_orb_9984_T",  "win_rate": 0.61, "daily_pnl_jpy": 3200},
                {"strategy": "jp_vwap_6758_T", "win_rate": 0.55, "daily_pnl_jpy": 1800},
            ],
        }, ensure_ascii=False),
        description="前場強気環境でのORB vs VWAP選択",
    ),
    TestCase(
        name="avoid_lunch_entry",
        user_input=json.dumps({
            "regime": "中立",
            "session_time": "11:25",
            "candidates": [
                {"symbol": "7203.T", "name": "Toyota", "orb_score": 0.55, "vwap_score": 0.50},
            ],
            "backtest_results": [
                {"strategy": "jp_orb_7203_T", "win_rate": 0.48, "daily_pnl_jpy": 500},
            ],
        }, ensure_ascii=False),
        description="前場クローズ直前の見送り判断",
    ),
],

"jp_pts_advisor": [
    TestCase(
        name="volume_spike_semiconductor",
        user_input=json.dumps({
            "pts_candidates": [
                {
                    "symbol": "8035.T", "name": "Tokyo Electron", "sector": "半導体",
                    "prev_volume_ratio": 2.8, "prev_range_pct": 3.5,
                    "prev_change_pct": 2.1, "trend_days": 3, "signal": "breakout_candidate",
                    "price": 28500,
                },
                {
                    "symbol": "6920.T", "name": "Lasertec", "sector": "半導体",
                    "prev_volume_ratio": 1.9, "prev_range_pct": 2.8,
                    "prev_change_pct": 1.6, "trend_days": 2, "signal": "momentum",
                    "price": 19200,
                },
            ]
        }, ensure_ascii=False),
        description="半導体株の出来高急増によるPTS候補分析",
    ),
    TestCase(
        name="bank_sector_pts",
        user_input=json.dumps({
            "pts_candidates": [
                {
                    "symbol": "8306.T", "name": "MUFG", "sector": "銀行",
                    "prev_volume_ratio": 2.1, "prev_range_pct": 1.8,
                    "prev_change_pct": 1.4, "trend_days": 5, "signal": "momentum",
                    "price": 1850,
                },
            ]
        }, ensure_ascii=False),
        description="銀行株のモメンタム継続シナリオ",
    ),
],

}


# ── Judge プロンプト ──────────────────────────────────────────────────────────

_JUDGE_SYSTEM = """あなたはAIエージェントのプロンプト品質を評価する審査員です。
与えられた「エージェントの出力」が「合格基準リスト」を満たしているか判定してください。

評価結果は必ず以下のJSON形式で返してください（説明文は不要）:
{
  "passed": [基準を満たしたものの番号リスト, 例: [0, 1, 3]],
  "failed": [基準を満たさなかったものの番号リスト, 例: [2]],
  "score":  0.0から1.0の総合スコア,
  "comment": "改善が必要な点を1文で"
}"""

_JUDGE_USER_TPL = """## エージェント出力
{output}

## 合格基準（インデックス付き）
{criteria}

上記の出力が各基準を満たしているか評価してください。"""


@dataclass
class EvalResult:
    test_name:  str
    passed:     list[int]
    failed:     list[int]
    score:      float
    comment:    str
    output:     str


def _format_criteria(criteria: list[str]) -> str:
    return "\n".join(f"[{i}] {c}" for i, c in enumerate(criteria))


async def evaluate_prompt(
    agent: AgentDef,
    prompt: str,
    client: anthropic.AsyncAnthropic,
) -> tuple[float, list[EvalResult]]:
    """プロンプトを全テストケースで評価し、平均スコアを返す。"""
    cases = TEST_CASES.get(agent.role, [])
    if not cases:
        logger.warning("No test cases for role %s", agent.role)
        return 0.0, []

    results: list[EvalResult] = []

    for tc in cases:
        # Step 1: エージェントにレスポンスを生成させる
        try:
            resp = await client.messages.create(
                model=agent.model,
                max_tokens=512,
                system=prompt,
                messages=[{"role": "user", "content": tc.user_input}],
            )
            agent_output = resp.content[0].text
        except Exception as e:
            logger.error("Agent call failed for %s/%s: %s", agent.role, tc.name, e)
            results.append(EvalResult(tc.name, [], list(range(len(agent.pass_criteria))),
                                      0.0, f"API error: {e}", ""))
            continue

        # Step 2: Judge モデルで合否判定
        criteria_str = _format_criteria(agent.pass_criteria)
        judge_user   = _JUDGE_USER_TPL.format(output=agent_output, criteria=criteria_str)

        try:
            judge_resp = await client.messages.create(
                model=MODEL_JUDGE,
                max_tokens=256,
                system=_JUDGE_SYSTEM,
                messages=[{"role": "user", "content": judge_user}],
            )
            raw = judge_resp.content[0].text.strip()
            # JSON抽出（```json ... ``` に包まれていることもある）
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            verdict: dict[str, Any] = json.loads(raw)
            results.append(EvalResult(
                test_name=tc.name,
                passed=verdict.get("passed", []),
                failed=verdict.get("failed", []),
                score=float(verdict.get("score", 0.0)),
                comment=verdict.get("comment", ""),
                output=agent_output,
            ))
        except Exception as e:
            logger.error("Judge call failed for %s/%s: %s", agent.role, tc.name, e)
            # 部分点: 出力があれば0.3
            results.append(EvalResult(tc.name, [], list(range(len(agent.pass_criteria))),
                                      0.3, f"Judge error: {e}", agent_output))

    avg_score = sum(r.score for r in results) / len(results) if results else 0.0
    return avg_score, results
