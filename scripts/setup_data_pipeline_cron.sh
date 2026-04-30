#!/bin/bash
# 1m データ蓄積 + 銘柄プロファイル更新 cron セットアップ (VPS 用)
#
# ユーザー指針 (2026-04-30 17:34):
#   これから 1 分足データも蓄積。銘柄カテゴライズで新規銘柄を最短マッチ。
#
# このスクリプトは VPS で実行する想定。ローカルでも動作するが、
# universe 全銘柄を毎日取得する負荷があるため通常は VPS に置く。
#
# 実行頻度:
#   - 1m スナップショット: 毎営業日 15:30 (ザラ場後)
#   - プロファイル更新:    毎週日曜 22:00 (週末バッチ)
#   - カテゴリ更新:        プロファイル更新後すぐ (連続実行)
#
# 用途:
#   ssh bullvps "bash /root/algo-trading-system/scripts/setup_data_pipeline_cron.sh"

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/algo-trading-system}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/logs/data_pipeline}"

mkdir -p "$LOG_DIR"

CRON_TMP="$(mktemp)"
crontab -l 2>/dev/null > "$CRON_TMP" || true

# 既存の data_pipeline 行を削除
grep -v "# data_pipeline" "$CRON_TMP" > "${CRON_TMP}.new" || true
mv "${CRON_TMP}.new" "$CRON_TMP"

# 1. 毎営業日 15:35 に 1m スナップショット
cat <<EOF >> "$CRON_TMP"

# data_pipeline: 毎営業日 15:35 に 1m データ蓄積
35 15 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN scripts/run_if_tse_trading_day.py $PYTHON_BIN scripts/daily_1m_snapshot.py >> $LOG_DIR/daily_1m_snapshot.log 2>&1

# data_pipeline: 毎週日曜 22:00 に銘柄プロファイル + カテゴリ更新
0 22 * * 0 cd $PROJECT_ROOT && $PYTHON_BIN scripts/build_full_universe_profile.py >> $LOG_DIR/profile_update.log 2>&1
5 22 * * 0 cd $PROJECT_ROOT && $PYTHON_BIN scripts/categorize_symbols.py >> $LOG_DIR/categorize.log 2>&1
EOF

crontab "$CRON_TMP"
rm -f "$CRON_TMP"

echo "==> cron 登録完了:"
crontab -l | grep "data_pipeline" -A 1 || echo "(なし)"
echo ""
echo "ログ出力先: $LOG_DIR/"
