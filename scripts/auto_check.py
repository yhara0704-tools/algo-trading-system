#!/usr/bin/env python3
"""自動チェック＆Pushover通知スクリプト.

使い方:
    python3 scripts/auto_check.py [morning|afternoon|hourly]

モード:
    morning   毎朝9:00 — 夜間バックテスト結果・Robust銘柄数・サービス状態
    afternoon 毎日15:35 — 日中取引セッション結果・BII書き込み確認
    hourly    毎時 — エラー監視（深刻なエラーのみ通知）
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── 設定 ────────────────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "algo_trading.db"
MACD_PARAMS = BASE_DIR / "data" / "macd_rci_params.json"
BEST_PARAMS  = BASE_DIR / "data" / "best_params.json"
BII_DAILY_DIR = Path("/root/algo_shared/daily")
LOG_LINES = 200  # journalctlで取得する行数

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def push(title: str, message: str, priority: int = 0) -> bool:
    user  = os.getenv("PUSHOVER_USER_KEY", "")
    token = os.getenv("PUSHOVER_API_TOKEN") or os.getenv("PUSHOVER_APP_TOKEN", "")
    if not user or not token:
        logger.warning("Pushover not configured")
        return False
    try:
        resp = httpx.post(PUSHOVER_URL, data={
            "token": token, "user": user,
            "title": title, "message": message, "priority": priority,
        }, timeout=10)
        resp.raise_for_status()
        logger.info("Pushover sent: %s", title)
        return True
    except Exception as exc:
        logger.warning("Pushover failed: %s", exc)
        return False


# ── サービス状態チェック ────────────────────────────────────────────────────────
def check_services() -> dict[str, str]:
    services = ["algo-trading.service", "backtest-daemon.service"]
    result = {}
    for svc in services:
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", svc],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            result[svc] = out
        except subprocess.CalledProcessError as e:
            result[svc] = e.output.strip() if e.output else "inactive"
    return result


# ── エラーログチェック ──────────────────────────────────────────────────────────
def get_recent_errors(minutes: int = 60) -> list[str]:
    """直近N分のERRORログを返す（重複除去）。"""
    try:
        since = f"{minutes} minutes ago"
        out = subprocess.check_output(
            ["journalctl", "-u", "algo-trading.service",
             "--since", since, "--no-pager", "-p", "err"],
            stderr=subprocess.DEVNULL, text=True
        )
        lines = [l.strip() for l in out.splitlines() if "ERROR" in l or "CRITICAL" in l]
        # 同一エラー文字列の重複除去
        seen: set[str] = set()
        unique: list[str] = []
        for l in lines:
            # タイムスタンプ部分を除いてdedup
            key = l[24:] if len(l) > 24 else l
            if key not in seen:
                seen.add(key)
                unique.append(l)
        return unique[:10]  # 最大10件
    except Exception:
        return []


# ── Robust銘柄チェック ──────────────────────────────────────────────────────────
def get_robust_symbols() -> list[str]:
    if not MACD_PARAMS.exists():
        return []
    data = json.loads(MACD_PARAMS.read_text())
    return [sym for sym, v in data.items() if v.get("robust")]


# ── DB: 最新日次サマリー ────────────────────────────────────────────────────────
def get_latest_daily_summary() -> dict:
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT date, jp_session_pnl, best_pnl_jpy, positive_strategies, total_strategies "
            "FROM daily_summaries ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return {}
        return {
            "date": row[0], "jp_pnl": row[1] or 0,
            "best_pnl": row[2] or 0, "positive": row[3], "total": row[4],
        }
    except Exception as e:
        logger.warning("DB read error: %s", e)
        return {}


# ── DB: 累積P&L ────────────────────────────────────────────────────────────────
def get_cumulative_pnl_jpy(days: int = 30) -> float:
    if not DB_PATH.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT jp_session_pnl FROM daily_summaries "
            "ORDER BY rowid DESC LIMIT ?", (days,)
        ).fetchall()
        conn.close()
        return sum(r[0] or 0 for r in rows)
    except Exception:
        return 0.0


# ── BII日次JSON確認 ────────────────────────────────────────────────────────────
def check_bii_daily(today: str) -> str:
    path = BII_DAILY_DIR / f"{today}.json"
    if path.exists():
        return f"書き込み済み ✓"
    return "未書き込み"


# ── backtest-daemon 進捗 ────────────────────────────────────────────────────────
def get_daemon_progress() -> str:
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "backtest-daemon.service",
             "--since", "3 hours ago", "--no-pager"],
            stderr=subprocess.DEVNULL, text=True
        )
        # Robust確定ログを抽出
        robust_lines = [l for l in out.splitlines() if "Robust" in l or "robust" in l.lower()]
        if robust_lines:
            return robust_lines[-1][-80:]  # 直近1件の末尾80文字
        # 直近ログ行
        lines = [l for l in out.splitlines() if l.strip()]
        return lines[-1][-80:] if lines else "ログなし"
    except Exception:
        return "取得失敗"


# ── モード: morning ────────────────────────────────────────────────────────────
def run_morning() -> None:
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    services = check_services()
    svc_lines = []
    dead_services = []
    for svc, status in services.items():
        icon = "✓" if status == "active" else "✗"
        name = svc.replace(".service", "")
        svc_lines.append(f"{icon} {name}: {status}")
        if status != "active":
            dead_services.append(name)

    robust = get_robust_symbols()
    summary = get_latest_daily_summary()
    cumulative = get_cumulative_pnl_jpy(30)
    bii = check_bii_daily(today)
    daemon_prog = get_daemon_progress()
    errors = get_recent_errors(minutes=480)  # 朝は8時間分

    lines = [
        f"📅 {today} 朝次チェック",
        "",
        "【サービス】",
        *svc_lines,
        "",
        f"【Robust銘柄】{len(robust)}件: {', '.join(robust) if robust else 'なし'}",
        "",
        "【昨日のP&L】",
        f"  JP live: {summary.get('jp_pnl', 0):+,.0f}円" if summary else "  データなし",
        f"  累計(30日): {cumulative:+,.0f}円",
        "",
        f"【BII日次JSON】{bii}",
        f"【バックテスト進捗】{daemon_prog}",
    ]

    if errors:
        lines += ["", f"【⚠️ エラー ({len(errors)}件)】", *[f"  {e[-60:]}" for e in errors[:3]]]

    if dead_services:
        # サービス死亡は高優先度
        push("🚨 VPS サービス停止", "\n".join(lines), priority=1)
    else:
        push("🌅 朝次レポート", "\n".join(lines), priority=-1)


# ── モード: afternoon ──────────────────────────────────────────────────────────
def run_afternoon() -> None:
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    summary = get_latest_daily_summary()
    bii = check_bii_daily(today)
    robust = get_robust_symbols()
    errors = get_recent_errors(minutes=120)  # 直近2時間

    jp_pnl = summary.get("jp_pnl", 0) if summary else 0
    pnl_icon = "📈" if jp_pnl >= 0 else "📉"

    lines = [
        f"📅 {today} 場後レポート",
        "",
        f"【JP live P&L】{pnl_icon} {jp_pnl:+,.0f}円",
        f"【Robust銘柄】{len(robust)}件: {', '.join(robust) if robust else 'なし'}",
        f"【BII日次JSON】{bii}",
    ]

    if errors:
        lines += ["", f"【⚠️ エラー ({len(errors)}件)】", *[f"  {e[-60:]}" for e in errors[:3]]]

    push("📊 場後レポート", "\n".join(lines), priority=0)


# ── モード: hourly ─────────────────────────────────────────────────────────────
def run_hourly() -> None:
    services = check_services()
    dead = [s for s, st in services.items() if st != "active"]
    errors = get_recent_errors(minutes=65)

    # どちらも問題なければ通知しない
    if not dead and not errors:
        logger.info("Hourly check: all OK")
        return

    lines = []
    if dead:
        lines.append(f"🚨 停止サービス: {', '.join(dead)}")
    if errors:
        lines.append(f"⚠️ エラー {len(errors)}件:")
        lines += [f"  {e[-70:]}" for e in errors[:5]]

    push("🚨 VPS異常検知", "\n".join(lines), priority=1)


# ── エントリーポイント ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # .env 読み込み
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    logger.info("auto_check.py mode=%s", mode)

    if mode == "morning":
        run_morning()
    elif mode == "afternoon":
        run_afternoon()
    elif mode == "hourly":
        run_hourly()
    else:
        print(f"Unknown mode: {mode}. Use morning / afternoon / hourly")
        sys.exit(1)
