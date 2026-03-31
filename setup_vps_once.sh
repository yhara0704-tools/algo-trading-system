#!/usr/bin/env bash
# setup_vps_once.sh — VPS 初回セットアップ（一度だけ実行）
# bull_system に干渉しない形で algo-trading-system を導入する
#
# 使い方:
#   chmod +x setup_vps_once.sh
#   ./setup_vps_once.sh
set -euo pipefail

VPS_HOST="${VPS_HOST:-bullvps}"

echo "=== VPS 初回セットアップ ==="
echo ""

# 1. VPS の Python バージョン確認
echo "[確認] VPS の Python バージョン:"
ssh "${VPS_HOST}" 'python3 --version && which python3'
echo ""

# 2. bull_system の使用ポートを確認（競合チェック）
echo "[確認] 使用中ポート (8000/8001):"
ssh "${VPS_HOST}" 'ss -tlnp | grep -E "8000|8001" || echo "  (競合なし)"'
echo ""

# 3. ディレクトリ作成
echo "[1/3] ディレクトリ作成..."
ssh "${VPS_HOST}" 'mkdir -p /root/algo-trading-system/data'
echo "  → /root/algo-trading-system/ 作成済み"

# 4. .env をコピー
echo ""
echo "[2/3] .env をコピー中..."
scp "$(dirname "${BASH_SOURCE[0]}")/.env" "${VPS_HOST}:/root/algo-trading-system/.env"
echo "  → .env コピー済み"
echo "  ⚠  VPS の .env 内の PORT を 8001 に変更します..."
ssh "${VPS_HOST}" "sed -i 's/^PORT=.*/PORT=8001/' /root/algo-trading-system/.env"
ssh "${VPS_HOST}" "sed -i 's/^HOST=.*/HOST=127.0.0.1/' /root/algo-trading-system/.env"
echo "  → PORT=8001 / HOST=127.0.0.1 に設定済み"

# 5. best_params.json を同期（パラメータ設定を引き継ぐ）
echo ""
echo "[3/3] best_params.json を同期中..."
LOCAL_PARAMS="$(dirname "${BASH_SOURCE[0]}")/data/best_params.json"
if [[ -f "${LOCAL_PARAMS}" ]]; then
    scp "${LOCAL_PARAMS}" "${VPS_HOST}:/root/algo-trading-system/data/best_params.json"
    echo "  → best_params.json コピー済み（最適パラメータ引き継ぎ）"
else
    echo "  → best_params.json なし（VPS 起動時に自動生成されます）"
fi

echo ""
echo "=== 初回セットアップ完了 ==="
echo ""
echo "次のステップ: ./deploy_vps.sh を実行してコードをデプロイしてください"
echo ""
