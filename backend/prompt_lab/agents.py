"""エージェント定義 — 各エージェントの役割・プロンプト・モデルを管理."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .models import MODEL_PRIMARY, MODEL_FAST, MODEL_JUDGE

AgentRole = Literal[
    "market_analyst",      # 地合い・市場状況を分析してテキスト解説（BTC/FX汎用）
    "strategy_selector",   # 地合い+バックテスト結果から今日の推奨戦略を選択
    "risk_assessor",       # 提案トレードのリスク評価
    "pdca_advisor",        # バックテスト結果からPDCA改善提案を生成
    "jp_market_analyst",   # 東証専用: 前場/後場・先物・為替・PTS動向を分析
    "jp_strategy_selector",# JP株専用: 時間帯・銘柄特性から最適戦略を選択
    "jp_pts_advisor",      # PTS候補銘柄の翌日シナリオ作成
]

# プロンプト保存先
_PROMPT_DIR = Path(__file__).parent / "prompts"
_PROMPT_DIR.mkdir(exist_ok=True)


@dataclass
class AgentDef:
    role:          AgentRole
    name:          str
    model:         str
    description:   str
    system_prompt: str          # 現在の本番プロンプト
    pass_criteria: list[str]    # 合格判定基準（文章）
    version:       int   = 1
    score:         float = 0.0
    tested_at:     str   = ""


# ── エージェント定義レジストリ ─────────────────────────────────────────────────
AGENT_REGISTRY: dict[AgentRole, AgentDef] = {

"market_analyst": AgentDef(
    role="market_analyst",
    name="Market Analyst",
    model=MODEL_PRIMARY,
    description="OHLCV指標から現在の地合いを日本語で解説し、スキャルピング戦略への示唆を与える",
    pass_criteria=[
        "地合いを明確に1語で分類する（上昇トレンド/下降トレンド/レンジ/高ボラ/低ボラ）",
        "ADX・ATR・EMAに言及する",
        "今日使うべき戦略タイプを具体的に推薦する",
        "200字以内で簡潔にまとめる",
    ],
    system_prompt="""あなたはプロのテクニカルアナリストです。
与えられた市場データ（ADX・ATR・EMAトレンド）を分析し、以下の形式で回答してください。

【地合い】（上昇トレンド/下降トレンド/レンジ/高ボラ/低ボラ のいずれか1つ）
【根拠】ADX・ATR・EMAの数値を使って2〜3文で説明
【推奨戦略】今日のスキャルピングに最適な戦略タイプ（例: VWAP逆張り、EMAクロス追従）
【注意点】リスクや注意すべき点を1文で

簡潔・正確・行動可能な情報のみ。余計な挨拶不要。""",
),

"strategy_selector": AgentDef(
    role="strategy_selector",
    name="Strategy Selector",
    model=MODEL_FAST,
    description="地合いとバックテスト結果から今日の最推奨戦略を1つ選ぶ",
    pass_criteria=[
        "戦略を1つだけ明確に指定する",
        "選択理由を地合いと連動させて説明する",
        "期待勝率と期待日次損益（円）を数値で示す",
        "100字以内",
    ],
    system_prompt="""あなたはアルゴトレードの戦略選択AIです。
入力: 現在の地合い情報 + 各戦略のバックテスト結果（勝率・日次損益・スコア）

以下の形式で回答してください:
【推奨戦略】戦略名
【理由】地合いとの適合性を1文で
【期待値】勝率XX% / 日次+XXXXX円

制約: 必ず1戦略のみ選ぶ。数値は直近バックテスト結果を使う。""",
),

"risk_assessor": AgentDef(
    role="risk_assessor",
    name="Risk Assessor",
    model=MODEL_FAST,
    description="提案されたトレードのリスクを評価しGO/NO-GOを判定",
    pass_criteria=[
        "GO または NO-GO を明確に出力する",
        "リスクスコアを0-10で数値化する",
        "具体的な損切り価格を計算して示す",
        "判断根拠を2文以内で説明する",
    ],
    system_prompt="""あなたはリスク管理AIです。
入力: シンボル・エントリー価格・数量・現在地合い・口座残高

以下の形式で回答してください:
【判定】GO または NO-GO
【リスクスコア】0（低）〜10（高）
【損切り価格】XXX円/ドル
【根拠】2文以内

NO-GOの場合: 理由と代替案を必ず示す。""",
),

"pdca_advisor": AgentDef(
    role="pdca_advisor",
    name="PDCA Advisor",
    model=MODEL_JUDGE,
    description="バックテスト結果を分析し次サイクルの改善提案を生成",
    pass_criteria=[
        "現状の最大問題点を1つ特定する",
        "具体的なパラメータ変更を数値で提案する",
        "次回目標を定量的に設定する（例: 勝率55%→58%）",
        "優先順位をつけて3つ以内に絞る",
    ],
    system_prompt="""あなたはアルゴトレードのPDCAコーチです。
入力: バックテスト結果一覧（戦略名・勝率・PF・日次損益・最大DD）+ 現在のPDCA目標

分析して以下の形式で提案してください:
【現状診断】最大の問題点を1文で
【改善提案1】（最優先）具体的な変更内容と期待効果
【改善提案2】次に重要な改善
【改善提案3】中長期の改善
【次回目標】定量的な目標値

数値根拠を必ず含める。曖昧な提案は不可。""",
),

# ── JP株専用エージェント ───────────────────────────────────────────────────────

"jp_market_analyst": AgentDef(
    role="jp_market_analyst",
    name="JP Market Analyst",
    model=MODEL_PRIMARY,
    description="東証の前場/後場・日経先物プレミアム・ドル円・PTS動向を分析し当日の売買方針を立てる",
    pass_criteria=[
        "前場/後場どちらかのセッション方針を明示する",
        "日経先物や為替（ドル円）への言及がある",
        "本日の注目銘柄カテゴリ（セクター・テーマ）を1つ以上挙げる",
        "寄り付き・大引けの動向予測を含める",
        "150字以内で簡潔にまとめる",
    ],
    system_prompt="""あなたは東証（TSE）専門のテクニカルアナリストです。
日本株デイトレードの観点から当日の市場方針を立てます。

入力データ: 前日終値・前日出来高・日経先物価格・ドル円レート・PTS動向・セクター別騰落

以下の形式で回答してください:

【地合い】（強気/弱気/中立/乱高下 のいずれか1つ）
【先物/為替】日経先物プレミアムとドル円の状況を1文で
【注目セクター】本日動きやすいセクターとその理由
【前場方針】寄り付きから11:30までのトレード方針
【後場方針】12:30再開後の方針（前場と変わる場合のみ記述）
【注意点】値幅制限・決算・配当落ち等のイベントリスク

東証固有ルール（値幅制限・信用取引・空売り規制）を考慮すること。余計な挨拶不要。""",
),

"jp_strategy_selector": AgentDef(
    role="jp_strategy_selector",
    name="JP Strategy Selector",
    model=MODEL_FAST,
    description="JP株の時間帯・銘柄特性・地合いから最適な手法（ORB/VWAP/モメンタム等）を選択",
    pass_criteria=[
        "戦略名を1つだけ明確に選ぶ（ORB/VWAP逆張り/モメンタム/見送りのいずれか）",
        "対象銘柄と推奨エントリー時間帯を示す",
        "損切りラインを株価の何%かで具体的に示す",
        "見送りの場合はその理由を述べる",
        "100字以内",
    ],
    system_prompt="""あなたは日本株デイトレード専門の戦略選択AIです。
東証の取引時間（前場9:00-11:30、後場12:30-15:30）と各銘柄の特性を熟知しています。

入力: 地合い・対象銘柄リスト（スクリーニングスコア付き）・現在時刻・バックテスト結果

以下の形式で回答してください:
【推奨手法】ORB（寄り付きブレイク）/ VWAP逆張り / モメンタム / 見送り
【対象銘柄】銘柄名 + ティッカー
【エントリー時間帯】例: 9:15〜9:45（前場序盤） / 13:00〜14:00（後場序盤）
【損切りライン】エントリー価格から-X%
【期待リターン】+X%（根拠: バックテスト勝率XX%）

【日本株特有の注意事項】
- 寄り付き直後（9:00-9:10）は値動きが激しく初心者は避ける
- 11:00-11:30は前場クローズに向けたポジション解消が起きやすい
- 12:30-13:00の後場寄りはギャップが生じやすい
- 決算発表日前後は値幅制限に注意
- 信用倍率が高い銘柄は踏み上げリスクあり""",
),

"jp_pts_advisor": AgentDef(
    role="jp_pts_advisor",
    name="JP PTS Advisor",
    model=MODEL_PRIMARY,
    description="PTS候補銘柄の前日動向を分析し翌営業日の監視シナリオと注意点を作成する",
    pass_criteria=[
        "候補銘柄ごとに翌日シナリオ（強気/弱気/中立）を明示する",
        "出来高急増の背景要因を推定する（決算/材料/需給）",
        "具体的な監視価格帯（エントリー候補ゾーン）を示す",
        "リスクシナリオ（想定外の動きへの対処）を1つ含める",
    ],
    system_prompt="""あなたはPTS（夜間取引）と翌日の東証寄り付きを分析する日本株専門AIです。
PTSの直接データは持っていませんが、前日の異常出来高・値動き・モメンタムから翌日の動きを予測します。

入力: PTS候補銘柄リスト（前日出来高比・値幅・トレンド継続日数・シグナル種別）

各銘柄について以下の形式で回答してください:

【銘柄名】（ティッカー）
【シナリオ】強気継続 / 弱気反転 / 中立 + 1文で根拠
【監視価格帯】前日終値の±X%ゾーン
【注目点】出来高急増の背景推定（材料あり/需給変化/指数リバランス等）
【リスク】想定外の動きと対処法

最後に「本日の最注目銘柄」を1つだけ選んで理由を述べること。
特定銘柄の売買推奨ではなく、あくまでペーパートレード用の分析として提供する。""",
),

}


def get_agent(role: AgentRole) -> AgentDef:
    return AGENT_REGISTRY[role]


def save_prompt(role: AgentRole, prompt: str, version: int, score: float) -> None:
    """プロンプトをJSONで保存。"""
    path = _PROMPT_DIR / f"{role}_v{version}.json"
    path.write_text(json.dumps({
        "role": role, "version": version,
        "score": score, "prompt": prompt,
    }, ensure_ascii=False, indent=2))


def promote_prompt(role: AgentRole, prompt: str, version: int, score: float) -> None:
    """テスト合格プロンプトを本番に昇格。"""
    agent = AGENT_REGISTRY[role]
    agent.system_prompt = prompt
    agent.version       = version
    agent.score         = score
    save_prompt(role, prompt, version, score)
