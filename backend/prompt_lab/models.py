"""AIモデル設定 — ここを変えるだけで全エージェントのモデルが更新される."""
from __future__ import annotations

# ── 現行モデル定義 ─────────────────────────────────────────────────────────────
# 最新モデルに更新する場合はここだけ変更する

# 主力モデル: 推論・分析タスク
MODEL_PRIMARY = "claude-sonnet-4-6"

# 高速モデル: 分類・短文生成タスク
MODEL_FAST = "claude-haiku-4-5-20251001"

# 最高性能モデル: プロンプト評価・改善提案
MODEL_JUDGE = "claude-opus-4-6"

# プロンプト最適化で使うモデル（候補生成 + 評価）
MODEL_OPTIMIZER = "claude-sonnet-4-6"

# ── モデルカタログ（参照用） ───────────────────────────────────────────────────
AVAILABLE_MODELS = {
    "claude-opus-4-6":          {"tier": "flagship", "context": 200_000},
    "claude-sonnet-4-6":        {"tier": "balanced", "context": 200_000},
    "claude-haiku-4-5-20251001":{"tier": "fast",     "context": 200_000},
}
