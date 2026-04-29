# 🚨 bullvps 移行のお知らせ (2026-04-29)

> **未来の自分 / 別セッションの Cursor へ。SSH で旧 VPS に入ろうとして「あれ？」となる前にこれを読んでください。**

## 何が起きた

2026-04-29 22:50 JST に、algo-trading-system / bull_forecast の本番稼働環境を **新 VPS（4 GB プラン）** に移行しました。
旧 VPS（1 GB プラン）でメモリ逼迫していた問題を解消し、加えて **VOICEVOX Docker を 24/7 ホスト** できるようにするためです。

## 接続先 (重要)

| エイリアス | IPv4 | Tailscale | 用途 |
|---|---|---|---|
| **`bullvps`** | **`160.251.252.13`** | **`100.103.193.57`** | **新 VPS（4GB）— ここで作業する** |
| `bullvps_new` | `160.251.252.13` | `100.103.193.57` | 上と同じ（明示用） |
| `bullvps_old` | `160.251.179.0` | `100.92.193.34` | **旧 VPS（1GB）— 2026-05-01 頃 削除予定** |

ローカル `~/.ssh/config` の `bullvps` エイリアスは既に新 VPS を指しています。  
**普通に `ssh bullvps` すれば新 VPS に入ります。**

## 旧 VPS の現状

- crontab: **全削除済み**（バックアップは `~/Backups/bullvps_20260429/crontab.bak` on Mac）
- systemd custom services（algo-trading / backtest-daemon / bull / crypto-arb）: **全 stop**
- データ: そのまま保持（新 VPS の動作確認 1〜2 日が問題なければ削除）
- SSH ログイン時に `/etc/motd` で警告メッセージが表示されます

⚠️ **旧 VPS で `crontab` 入れ直しや `systemctl start` は絶対にしないでください。**  
新旧両方の cron / systemd が走ると、Pushover 通知二重送信・X API 二重投稿・ファイル競合などの事故になります。

## 新 VPS の追加機能

- **VOICEVOX Docker (CPU版)** が `localhost:50021` で常時稼働。  
  → ずんだもん (id=3) によるポッドキャスト・YouTube ショート音声合成が **PC を開かなくても完全自動化**。
- メモリ 1 GB → **3.8 GB** （Swap 込み 5.8 GB）

## 完全バックアップの場所（万一のロールバック用）

Mac ローカル: `~/Backups/bullvps_20260429/`（921 MB）
- `bull_forecast.tar.gz` (835 MB)
- `algo-trading-system.tar.gz` (57 MB)
- `bull_system.tar.gz` (11 MB)
- `algo_shared.tar.gz` (3.6 MB)
- `crontab.bak` / `systemd_units/` / `sshkeys/` / `bashrc.bak` / `apt_selections.txt` ほか
- `setup_new_bullvps.sh`（再現可能な自動セットアップスクリプト）

## トラブル時のロールバック手順

万一新 VPS で致命的な問題が出たら:

```bash
# 1. ~/.ssh/config の bullvps を旧に戻す（HostName 160.251.179.0）
# 2. 旧 VPS で復旧
ssh bullvps_old
crontab ~/.bashrc.bak  # ← バックアップから元に戻す手順
systemctl start algo-trading.service backtest-daemon.service bull.service
# (※ crontab.bak は ~/Backups/bullvps_20260429/crontab.bak をリストア)
```

## 既知の例外

- `crypto-arb.service` は旧 VPS でも `scripts/crypto_arb_monitor.py` が無く壊れていたため、新 VPS では `disabled`。今回の移行とは無関係。

---

**作業時の鉄則: `ssh bullvps` で新 VPS に入ること。`ssh bullvps_old` を打つときは何をしているか自覚すること。**
