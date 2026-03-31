#!/usr/bin/env bash
# deploy_vps.sh — VPS へコードをデプロイして systemd サービスを再起動する
#
# 使い方:
#   chmod +x deploy_vps.sh
#   ./deploy_vps.sh
#
# 前提:
#   - SSH エイリアス "bullvps" が ~/.ssh/config に設定済み
#     (または VPS_HOST を変更)
#   - VPS に /root/algo-trading-system/ を作成済み (初回は自動作成)
#   - VPS に .env をコピー済み (初回セットアップを参照)
set -euo pipefail

VPS_HOST="${VPS_HOST:-bullvps}"
REMOTE_DIR="/root/algo-trading-system"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Algo Trading → VPS デプロイ ==="
echo "  送信先: ${VPS_HOST}:${REMOTE_DIR}"
echo ""

# コードを rsync（data/ と .venv/ と __pycache__ は除外）
echo "[1/4] コードを同期中..."
rsync -avz --delete \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='.git/' \
  "${LOCAL_DIR}/" "${VPS_HOST}:${REMOTE_DIR}/"

echo ""
echo "[2/4] 依存パッケージをインストール中..."
ssh "${VPS_HOST}" bash <<'REMOTE'
  set -euo pipefail
  cd /root/algo-trading-system
  # venv がなければ作成
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
    echo "  → .venv 作成済み"
  fi
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r backend/requirements.txt
  echo "  → パッケージ OK"
REMOTE

echo ""
echo "[3/4] systemd サービスをインストール中..."
ssh "${VPS_HOST}" bash <<'REMOTE'
  # サービスファイルをコピー
  cp /root/algo-trading-system/algo-trading.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable algo-trading
  echo "  → サービス登録 OK"
REMOTE

echo ""
echo "[4/4] サービスを再起動中..."
ssh "${VPS_HOST}" bash <<'REMOTE'
  systemctl restart algo-trading
  sleep 3
  systemctl status algo-trading --no-pager | head -20
REMOTE

echo ""
echo "=== デプロイ完了 ==="
echo ""
echo "  ログ確認:  ssh ${VPS_HOST} 'journalctl -u algo-trading -f'"
echo "  UI アクセス: ssh -L 8001:localhost:8001 ${VPS_HOST}"
echo "             → ブラウザで http://localhost:8001/lab"
echo ""
