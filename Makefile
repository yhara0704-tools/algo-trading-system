# VPS へコードのみ同期（本番 data / .env は上書きしない）
VPS_HOST ?= bullvps
VPS_PATH ?= /root/algo-trading-system

# ローカル開発: http://localhost:8000 （./run.sh が .env の HOST/PORT を読み込む）
.PHONY: dev
dev:
	./run.sh

RSYNC_EXCLUDES := \
	--exclude '.git' \
	--exclude '.venv' \
	--exclude '__pycache__' \
	--exclude '*.pyc' \
	--exclude 'data/' \
	--exclude '.env' \
	--exclude '.cursor' \
	--exclude 'tmp/'

# ペーパー選定に必要な JSON のみ Mac→VPS（存在するファイルだけ送る。algo_trading.db 等は送らない）
# NOTE: jp_paper_trading_halt.json はローカルで paused_pairs を更新したら即時反映が必要。
#       deploy-vps は data/ を一括除外するため、これを忘れると paused_pair の until 期限が
#       VPS で古いままになり自動解除されてしまう (2026-04-30 の同期不全で実害あり)。
PAPER_DATA_FILES := \
	data/universe_active.json \
	data/universe_rotation_state.json \
	data/jp_paper_trading_halt.json

.PHONY: deploy-vps deploy-vps-dry sync-vps-paper-data

# 実送信（.env は常に除外 — VPS 上の本番キーを誤って上書きしない）
deploy-vps:
	rsync -avz -e ssh $(RSYNC_EXCLUDES) ./ $(VPS_HOST):$(VPS_PATH)/

# 差分確認のみ（送信しない）
deploy-vps-dry:
	rsync -avzn -e ssh $(RSYNC_EXCLUDES) ./ $(VPS_HOST):$(VPS_PATH)/

sync-vps-paper-data:
	@set -e; for f in $(PAPER_DATA_FILES); do \
	  if [ -f "$$f" ]; then \
	    rsync -avz -e ssh "$$f" "$(VPS_HOST):$(VPS_PATH)/$$f"; \
	    echo "synced $$f"; \
	  else echo "skip (not on Mac): $$f"; \
	  fi; \
	done
