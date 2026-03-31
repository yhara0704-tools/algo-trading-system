"""SQLite 永続ストレージ — バックテスト集約・サブセッション・日次サマリーを管理。

設計方針:
  - 細粒度 RunRecord は保持しない → 日次集約のみ (1戦略×1日 = 1行)
  - JP株サブセッション: 90日保持後パージ
  - 日次サマリー: 365日保持後パージ
  - strategy_knowledge.json の regime_stats はそのまま維持 (集約済み「脳」)

テーブル:
  backtest_daily_agg  1行 = 1戦略×1日の最良結果
  jp_subsessions      JP株リアル・サブセッション記録
  daily_summaries     夜間 Pushover サマリーのアーカイブ
  pts_screening       PTS スクリーニング履歴
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "algo_trading.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# スレッドセーフのためロックを持つ
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 読み書き同時アクセス対応
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _tx():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS backtest_daily_agg (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,          -- YYYY-MM-DD
    strategy_id     TEXT    NOT NULL,
    strategy_name   TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    interval        TEXT    NOT NULL,
    regime          TEXT    NOT NULL DEFAULT '',
    days_tested     INTEGER NOT NULL DEFAULT 0,
    num_trades      INTEGER NOT NULL DEFAULT 0,
    win_rate        REAL    NOT NULL DEFAULT 0,
    profit_factor   REAL    NOT NULL DEFAULT 0,
    daily_pnl_jpy   REAL    NOT NULL DEFAULT 0,
    max_drawdown_pct REAL   NOT NULL DEFAULT 0,
    sharpe          REAL    NOT NULL DEFAULT 0,
    score           REAL    NOT NULL DEFAULT 0,
    UNIQUE(date, strategy_id)    -- 1日1戦略1行
);

CREATE TABLE IF NOT EXISTS jp_subsessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    start_time      TEXT    NOT NULL,
    end_time        TEXT,
    reason          TEXT    NOT NULL DEFAULT '',
    num_trades      INTEGER NOT NULL DEFAULT 0,
    win_rate        REAL    NOT NULL DEFAULT 0,
    pnl_jpy         REAL    NOT NULL DEFAULT 0,
    strategies_used TEXT    NOT NULL DEFAULT '[]',  -- JSON list
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL UNIQUE,
    summary_text    TEXT    NOT NULL,
    total_strategies INTEGER NOT NULL DEFAULT 0,
    positive_strategies INTEGER NOT NULL DEFAULT 0,
    best_strategy   TEXT    NOT NULL DEFAULT '',
    best_pnl_jpy    REAL    NOT NULL DEFAULT 0,
    btc_regime      TEXT    NOT NULL DEFAULT '',
    jp_session_pnl  REAL    NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pts_screening (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    sector          TEXT    NOT NULL DEFAULT '',
    volume_ratio    REAL    NOT NULL DEFAULT 0,
    range_pct       REAL    NOT NULL DEFAULT 0,
    signal          TEXT    NOT NULL DEFAULT '',
    pts_score       REAL    NOT NULL DEFAULT 0,
    selected        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agg_date     ON backtest_daily_agg(date);
CREATE INDEX IF NOT EXISTS idx_agg_strategy ON backtest_daily_agg(strategy_id);
CREATE INDEX IF NOT EXISTS idx_sub_date     ON jp_subsessions(date);
CREATE INDEX IF NOT EXISTS idx_pts_date     ON pts_screening(date);
"""


def init_db() -> None:
    with _tx() as conn:
        conn.executescript(_DDL)
    logger.info("SQLite DB initialized: %s", _DB_PATH)


# ── Write helpers ──────────────────────────────────────────────────────────────

def upsert_backtest_agg(result: dict, regime: str = "") -> None:
    """バックテスト結果を日次集約テーブルに upsert する。
    同じ (date, strategy_id) が既にある場合はスコアが高ければ上書き。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with _tx() as conn:
        existing = conn.execute(
            "SELECT score FROM backtest_daily_agg WHERE date=? AND strategy_id=?",
            (today, result.get("strategy_id", ""))
        ).fetchone()

        new_score = result.get("score", 0.0)
        if existing and existing["score"] >= new_score:
            return   # 既存の方がスコア高い → スキップ

        conn.execute("""
            INSERT INTO backtest_daily_agg
                (date, strategy_id, strategy_name, symbol, interval, regime,
                 days_tested, num_trades, win_rate, profit_factor,
                 daily_pnl_jpy, max_drawdown_pct, sharpe, score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, strategy_id) DO UPDATE SET
                regime=excluded.regime,
                days_tested=excluded.days_tested,
                num_trades=excluded.num_trades,
                win_rate=excluded.win_rate,
                profit_factor=excluded.profit_factor,
                daily_pnl_jpy=excluded.daily_pnl_jpy,
                max_drawdown_pct=excluded.max_drawdown_pct,
                sharpe=excluded.sharpe,
                score=excluded.score
        """, (
            today,
            result.get("strategy_id", ""),
            result.get("strategy_name", ""),
            result.get("symbol", ""),
            result.get("interval", ""),
            regime,
            result.get("days_tested", 0),
            result.get("num_trades", 0),
            result.get("win_rate", 0.0),
            result.get("profit_factor", 0.0),
            result.get("daily_pnl_jpy", 0.0),
            result.get("max_drawdown_pct", 0.0),
            result.get("sharpe", 0.0),
            new_score,
        ))


def save_jp_subsession(date: str, start: str, end: str | None,
                       reason: str, num_trades: int, win_rate: float,
                       pnl_jpy: float, strategies: list[str]) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO jp_subsessions
                (date, start_time, end_time, reason, num_trades, win_rate, pnl_jpy, strategies_used)
            VALUES (?,?,?,?,?,?,?,?)
        """, (date, start, end, reason, num_trades, win_rate, pnl_jpy, json.dumps(strategies)))


def save_daily_summary(date: str, text: str, stats: dict) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO daily_summaries
                (date, summary_text, total_strategies, positive_strategies,
                 best_strategy, best_pnl_jpy, btc_regime, jp_session_pnl)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                summary_text=excluded.summary_text,
                total_strategies=excluded.total_strategies,
                positive_strategies=excluded.positive_strategies,
                best_strategy=excluded.best_strategy,
                best_pnl_jpy=excluded.best_pnl_jpy,
                btc_regime=excluded.btc_regime,
                jp_session_pnl=excluded.jp_session_pnl
        """, (
            date,
            text,
            stats.get("total_strategies", 0),
            stats.get("positive_strategies", 0),
            stats.get("best_strategy", ""),
            stats.get("best_pnl_jpy", 0.0),
            stats.get("btc_regime", ""),
            stats.get("jp_session_pnl", 0.0),
        ))


def save_pts_screening(date: str, candidates: list[dict]) -> None:
    with _tx() as conn:
        # 当日分は一度削除して入れ直し
        conn.execute("DELETE FROM pts_screening WHERE date=?", (date,))
        for c in candidates:
            conn.execute("""
                INSERT INTO pts_screening
                    (date, symbol, name, sector, volume_ratio, range_pct, signal, pts_score, selected)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                date, c.get("symbol",""), c.get("name",""), c.get("sector",""),
                c.get("prev_volume_ratio",0), c.get("prev_range_pct",0),
                c.get("signal",""), c.get("pts_score",0),
                1 if c.get("selected") else 0,
            ))


# ── Read helpers ───────────────────────────────────────────────────────────────

def get_strategy_history(strategy_id: str, days: int = 90) -> list[dict]:
    """戦略の日次パフォーマンス履歴を返す。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_daily_agg
            WHERE strategy_id=? AND date>=?
            ORDER BY date ASC
        """, (strategy_id, since)).fetchall()
    return [dict(r) for r in rows]


def get_all_strategies_latest() -> list[dict]:
    """全戦略の最新日のパフォーマンスを返す。"""
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_daily_agg
            WHERE date = (SELECT MAX(date) FROM backtest_daily_agg)
            ORDER BY score DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_daily_summaries(days: int = 30) -> list[dict]:
    """直近N日分の日次サマリーを返す。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM daily_summaries WHERE date>=? ORDER BY date DESC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def get_jp_subsessions(date: str | None = None) -> list[dict]:
    """JP株サブセッション履歴を返す。dateなら当日のみ。"""
    with _tx() as conn:
        if date:
            rows = conn.execute(
                "SELECT * FROM jp_subsessions WHERE date=? ORDER BY start_time",
                (date,)
            ).fetchall()
        else:
            since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT * FROM jp_subsessions WHERE date>=? ORDER BY date DESC, start_time",
                (since,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_daily_best_pnl(days: int = 14) -> list[dict]:
    """直近N日間の日別最良 daily_pnl_jpy を返す（本番移行判断用）。
    各日の JP株スキャル戦略のうち最高スコアの PnL を抽出する。
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT date,
                   MAX(daily_pnl_jpy)  AS best_pnl_jpy,
                   MAX(win_rate)        AS best_win_rate,
                   MAX(score)           AS best_score,
                   strategy_name
            FROM backtest_daily_agg
            WHERE date >= ? AND symbol LIKE '%.T'
            GROUP BY date
            ORDER BY date ASC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def get_milestone_progress() -> dict:
    """日付別の最良スコア戦略 daily_pnl_jpy を返す（マイルストーン曲線用）。"""
    with _tx() as conn:
        rows = conn.execute("""
            SELECT date, MAX(daily_pnl_jpy) as best_pnl, AVG(daily_pnl_jpy) as avg_pnl,
                   SUM(CASE WHEN daily_pnl_jpy > 0 THEN 1 ELSE 0 END) as positive_count,
                   COUNT(*) as total_count
            FROM backtest_daily_agg
            GROUP BY date ORDER BY date ASC
        """).fetchall()
    return {"history": [dict(r) for r in rows]}


# ── Pruning ────────────────────────────────────────────────────────────────────

def prune_old_data(
    agg_keep_days: int = 365,
    subsession_keep_days: int = 90,
    summary_keep_days: int = 365,
    pts_keep_days: int = 90,
) -> dict:
    """古いデータを削除してストレージを節約する。週次実行を推奨。"""
    cutoffs = {
        "backtest_daily_agg": (datetime.now() - timedelta(days=agg_keep_days)).strftime("%Y-%m-%d"),
        "jp_subsessions":     (datetime.now() - timedelta(days=subsession_keep_days)).strftime("%Y-%m-%d"),
        "daily_summaries":    (datetime.now() - timedelta(days=summary_keep_days)).strftime("%Y-%m-%d"),
        "pts_screening":      (datetime.now() - timedelta(days=pts_keep_days)).strftime("%Y-%m-%d"),
    }
    deleted = {}
    with _tx() as conn:
        for table, cutoff in cutoffs.items():
            col = "created_at" if table in ("jp_subsessions", "pts_screening") else "date"
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
            deleted[table] = cur.rowcount
        conn.execute("VACUUM")   # ファイルサイズを実際に縮小
    logger.info("Prune complete: %s", deleted)
    return deleted


# ── RunRecord 置き換え用の知識ベース統合 ──────────────────────────────────────

def migrate_knowledge_base_records() -> int:
    """strategy_knowledge.json の古い RunRecord を DB に移行し JSON から削除する。
    最新30件だけ JSON に残し、それ以前は DB に書き出す。
    移行済み件数を返す。
    """
    import json as _json
    from pathlib import Path

    kb_path = Path(__file__).parent.parent.parent / "data" / "strategy_knowledge.json"
    if not kb_path.exists():
        return 0

    with open(kb_path) as f:
        data = _json.load(f)

    migrated = 0
    for sid, strategy in data.items():
        records = strategy.get("records", [])
        if len(records) <= 30:
            continue

        keep   = records[-30:]   # 最新30件をJSONに残す
        archive = records[:-30]  # それ以外をDBへ

        with _tx() as conn:
            for rec in archive:
                date = datetime.fromtimestamp(rec.get("ts", 0)).strftime("%Y-%m-%d")
                conn.execute("""
                    INSERT OR IGNORE INTO backtest_daily_agg
                        (date, strategy_id, strategy_name, symbol, interval, regime,
                         days_tested, num_trades, win_rate, profit_factor,
                         daily_pnl_jpy, max_drawdown_pct, sharpe, score)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    date, sid, strategy.get("strategy_name", sid),
                    strategy.get("symbol", ""), strategy.get("interval", ""),
                    rec.get("regime", ""), rec.get("days", 0),
                    rec.get("num_trades", 0), rec.get("win_rate", 0),
                    rec.get("profit_factor", 0), rec.get("daily_pnl_jpy", 0),
                    rec.get("max_drawdown_pct", 0), rec.get("sharpe", 0),
                    rec.get("score", 0),
                ))
                migrated += 1

        strategy["records"] = keep

    with open(kb_path, "w") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info("Migrated %d old RunRecords to SQLite", migrated)
    return migrated


# ── Singleton init ─────────────────────────────────────────────────────────────
_initialized = False

def get_db() -> None:
    """起動時に1回呼ぶ。"""
    global _initialized
    if not _initialized:
        init_db()
        _initialized = True
