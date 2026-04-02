"""手法研究室 — Claude APIによる検証結果分析と次の仮説生成.

フロー:
  1. 検証結果（strategy_fit_map.json, macd_rci_params.json 等）を読み込む
  2. Claude API に渡して分析・考察・次の仮説を生成させる
  3. 仮説を lab_hypotheses.json に保存
  4. backtest_daemon.py が仮説を拾って実行する

実行:
    .venv/bin/python3 scripts/strategy_lab.py
    # または daemon から自動呼び出し
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# .env から環境変数を読み込む（nohup 起動時に未ロードの場合に備えて）
_env_path = pathlib.Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR       = pathlib.Path(__file__).parent.parent / "data"
FIT_MAP        = DATA_DIR / "strategy_fit_map.json"
PARAMS_FILE    = DATA_DIR / "macd_rci_params.json"
POOL_RESULT    = DATA_DIR / "scan_full_pool_result.json"
HYPOTHESES     = DATA_DIR / "lab_hypotheses.json"
LAB_LOG        = DATA_DIR / "lab_log.json"

# 実装済み戦略一覧（デーモンが解釈できるもの）
AVAILABLE_STRATEGIES = [
    "MacdRci",       # MACD(3,7,signal)×RCI(10,12,15) — tp/sl/rci_min_agree/macd_signal
    "Breakout",      # 前日高値ブレイクアウト
    "Scalp",         # 5分スキャルプ（only_slots/avoid_slots 対応）
    "Momentum5Min",  # 5分足モメンタム
    "ORB",           # Opening Range Breakout（最初15分のレンジ）
    "VwapReversion", # VWAP乖離率逆張り
]

# デーモンが解釈できる仮説タイプ
HYPOTHESIS_TYPES = {
    "macd_rci_grid":       "MACD×RCIのパラメータ範囲を変えてグリッドサーチ",
    "strategy_on_symbol":  "特定銘柄に特定手法をIS/OOSで検証",
    "time_window":         "特定時間帯（朝/昼前後/午後）に絞ったScalp検証",
    "multi_symbol_group":  "セクター・特性でグルーピングした銘柄群の一括検証",
    "regime_condition":    "相場環境（上昇/下降/レンジ/高ボラ）に限定した検証",
}


def _load_results() -> dict:
    """全検証結果を読み込んでサマリー辞書を返す。"""
    out = {}

    if FIT_MAP.exists():
        try:
            out["strategy_fit"] = json.loads(FIT_MAP.read_text())
        except Exception:
            pass

    if PARAMS_FILE.exists():
        try:
            out["macd_rci_params"] = json.loads(PARAMS_FILE.read_text())
        except Exception:
            pass

    if POOL_RESULT.exists():
        try:
            raw = json.loads(POOL_RESULT.read_text())
            out["pool_scan"] = raw.get("results", [])
        except Exception:
            pass

    return out


def _build_prompt(results: dict) -> str:
    """Claude に渡すプロンプトを構築する。"""

    # strategy_fit から主要な銘柄×手法マトリクスを文字列化
    fit_lines = []
    for sym, data in (results.get("strategy_fit") or {}).items():
        name = data.get("name", sym)
        best = data.get("best_strategy", "?")
        best_daily = data.get("best_is_daily", 0)
        strats = data.get("strategies", {})
        robust_strats = [k for k, v in strats.items() if v.get("robust")]
        fit_lines.append(
            f"  {name}({sym}): ベスト手法={best}(IS {best_daily:+,.0f}円/日)"
            + (f" Robust確定={robust_strats}" if robust_strats else " Robust=なし")
        )

    # macd_rci params から Robust確定銘柄
    robust_syms = []
    for sym, p in (results.get("macd_rci_params") or {}).items():
        if p.get("robust"):
            robust_syms.append(
                f"  {sym}: tp={p['tp_pct']} sl={p['sl_pct']} rci={p['rci_min_agree']} sig={p['macd_signal']}"
                f" IS {p.get('is_daily', 0):+,.0f} / OOS {p.get('oos_daily', 0):+,.0f}"
            )

    # pool scan サマリー
    pool = results.get("pool_scan") or []
    positive = [r for r in pool if not r.get("skip") and r.get("is_daily", 0) > 0]
    ng = [r for r in pool if not r.get("skip") and r.get("is_daily", 0) <= 0]
    skipped = [r for r in pool if r.get("skip")]

    pool_summary = (
        f"全銘柄スキャン: {len(pool)}件 "
        f"(IS陽性={len(positive)}, IS陰性={len(ng)}, スキップ={len(skipped)})\n"
        "IS陽性銘柄: "
        + ", ".join(f"{r['name']}({r.get('is_daily', 0):+,.0f}円)" for r in sorted(positive, key=lambda x: -x.get("is_daily", 0))[:10])
    )

    # 既存仮説（消化済み・未消化）
    existing = []
    if HYPOTHESES.exists():
        try:
            hyps = json.loads(HYPOTHESES.read_text())
            existing = [f"  [{h['status']}] {h['hypothesis_id']}: {h['description']}" for h in hyps[-10:]]
        except Exception:
            pass

    prompt = f"""あなたはアルゴリズムトレーディングの研究者です。
以下の検証結果を分析し、次に試すべき仮説を生成してください。

## システム概要
- 日本株デイトレード（松井証券 一日信用、手数料0円）
- 9:00-15:20の間のみ取引（日跨ぎ禁止）
- 資金: 30万円 × 3.3倍信用 ≈ 99万円
- ポジションサイズ: 33%/銘柄
- 株価フィルター: ≤3,267円/株（100株単位で取引可能な範囲）
- バックテスト方式: IS（新しい30日）/ OOS（古い30日）で過学習チェック
  Robust = IS日次プラス かつ OOS日次プラス

## 実装済み手法
{chr(10).join(f"- {s}: {HYPOTHESIS_TYPES.get(s, '')}" for s in AVAILABLE_STRATEGIES)}

## MACD×RCI Robust確定パラメータ（現在の本番設定）
{chr(10).join(robust_syms) if robust_syms else "  なし"}

## 全手法横断比較マトリクス（銘柄×手法）
{chr(10).join(fit_lines) if fit_lines else "  まだ結果なし"}

## 全銘柄スキャンサマリー（MACD×RCIデフォルト）
{pool_summary}

## 既に生成済みの仮説（直近10件）
{chr(10).join(existing) if existing else "  なし"}

---
以上を踏まえて、次に実施すべき検証仮説を5〜8件生成してください。

## 過学習防止ルール（必ず守ること）

**仮説を生成する前に以下を自問してください:**
1. この仮説は「既存の結果をさらに最適化する」のか、「新しい独立した視点から検証する」のか？
   → 前者は過学習リスクが高い。後者を優先すること。

2. サンプルサイズは十分か？
   → 時間帯フィルター・相場環境フィルターをかけると取引回数が激減する。
   → 期待取引回数が IS期間で20回未満になりそうな仮説は生成しないこと。

3. 「IS+OOSの両方でプラス」を唯一の昇格基準とすること。
   → IS単独で良くてもOOSが悪ければ過学習。仮説の目的は「OOSでも効くロジック」の発見。

4. 同じデータで繰り返し検証しない。
   → 既にRobust確定しているパラメータをさらに微調整する仮説は不要。
   → IS陰性（損失）銘柄に「別の工夫」を重ねるのは多重検定問題になるので慎重に。

5. シンプルな仮説を優先する。
   → 「条件を増やすほど過学習しやすい」という原則を常に意識する。
   → 例: 「朝だけ×高ボラ環境だけ×特定銘柄だけ」は過学習の典型。

## 仮説を生成すべき方向性（過学習リスク低）
1. まだ全く試していない手法×銘柄の組み合わせ（新しいデータポイント）
2. 「IS陽性・OOS陰性」の銘柄への「なぜOOSで崩れたか」の仮説検証（パラメータ数を増やさずに）
3. 複数銘柄にまたがる共通エッジの探索（セクター単位、流動性帯単位）
4. 手法自体のシンプルな変形（例: ORBのレンジ時間を15分→30分）で1パラメータのみ変更
5. 「現在NGの銘柄は本当にこの手法と相性が悪いのか」の確認（別手法で試す）

出力は必ず以下のJSON形式で返してください（他のテキストは不要）:
```json
[
  {{
    "hypothesis_id": "h{date.today().strftime('%Y%m%d')}_001",
    "type": "strategy_on_symbol",
    "description": "仮説の説明（日本語、1-2文）",
    "rationale": "この仮説を試す理由（検証結果から導いた根拠、2-3文）",
    "symbol": "銘柄コード.T または null（複数銘柄の場合）",
    "symbols": ["銘柄コード.T", ...],
    "strategy": "手法名（AVAILABLE_STRATEGIESから）",
    "params": {{"パラメータ名": 値}},
    "priority": 1,
    "status": "pending"
  }},
  ...
]
```

typeは以下から選んでください:
{json.dumps(HYPOTHESIS_TYPES, ensure_ascii=False, indent=2)}
"""
    return prompt


def _load_existing_hypotheses() -> list[dict]:
    if HYPOTHESES.exists():
        try:
            return json.loads(HYPOTHESES.read_text())
        except Exception:
            pass
    return []


def _save_hypotheses(new_hypotheses: list[dict]) -> None:
    existing = _load_existing_hypotheses()
    # 既存IDと重複しないものだけ追加
    existing_ids = {h["hypothesis_id"] for h in existing}
    added = [h for h in new_hypotheses if h["hypothesis_id"] not in existing_ids]
    all_hyps = existing + added
    HYPOTHESES.write_text(json.dumps(all_hyps, ensure_ascii=False, indent=2))
    logger.info("%d件の新仮説を追加（累計%d件）", len(added), len(all_hyps))
    return added


def _log_lab_run(num_generated: int, summary: str) -> None:
    log = []
    if LAB_LOG.exists():
        try:
            log = json.loads(LAB_LOG.read_text())
        except Exception:
            pass
    log.append({
        "timestamp": datetime.now().isoformat(),
        "num_generated": num_generated,
        "summary": summary,
    })
    LAB_LOG.write_text(json.dumps(log[-30:], ensure_ascii=False, indent=2))


def run_lab() -> list[dict]:
    """手法研究室を1サイクル実行し、新しい仮説リストを返す。"""
    logger.info("=== 手法研究室 起動 ===")

    results = _load_results()
    if not results:
        logger.warning("検証結果が見つかりません。先にデーモンを実行してください。")
        return []

    prompt = _build_prompt(results)

    logger.info("Claude API に分析・仮説生成を依頼中...")
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    logger.info("Claude応答受信 (%d文字)", len(raw))

    # JSON部分を抽出
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        hypotheses = json.loads(raw)
        if not isinstance(hypotheses, list):
            raise ValueError("リスト形式でない")
    except Exception as e:
        logger.error("仮説のJSONパース失敗: %s\n応答: %s", e, raw[:500])
        return []

    added = _save_hypotheses(hypotheses)
    summary = " / ".join(f"{h['hypothesis_id']}:{h['description'][:20]}" for h in (added or hypotheses)[:3])
    _log_lab_run(len(added or hypotheses), summary)

    logger.info("=== 手法研究室 完了 — 新仮説%d件 ===", len(added or hypotheses))
    return added or hypotheses


if __name__ == "__main__":
    new_hyps = run_lab()
    print(f"\n生成された仮説 ({len(new_hyps)}件):")
    for h in new_hyps:
        print(f"  [{h['priority']}] {h['hypothesis_id']}: {h['description']}")
        print(f"      根拠: {h['rationale'][:80]}...")
