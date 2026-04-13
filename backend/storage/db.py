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

-- ── TOB監視 ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tob_filings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id             TEXT    NOT NULL UNIQUE,
    date               TEXT    NOT NULL,
    doc_description    TEXT    NOT NULL DEFAULT '',
    filer_name         TEXT    NOT NULL DEFAULT '',
    issuer_edinet_code TEXT    NOT NULL DEFAULT '',
    amendment_flag     TEXT    NOT NULL DEFAULT '0',
    parent_doc_id      TEXT,
    form_code          TEXT    NOT NULL DEFAULT '',
    filing_type        TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tob_scores (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,
    issuer_edinet_code TEXT    NOT NULL,
    issuer_name        TEXT    NOT NULL DEFAULT '',
    sec_code           TEXT    NOT NULL DEFAULT '',
    total_filings_6m   INTEGER NOT NULL DEFAULT 0,
    amendment_count    INTEGER NOT NULL DEFAULT 0,
    unique_filers      INTEGER NOT NULL DEFAULT 0,
    has_old_amendment  INTEGER NOT NULL DEFAULT 0,
    pbr                REAL,
    market_cap_b       REAL,
    score              REAL    NOT NULL DEFAULT 0,
    score_detail       TEXT    NOT NULL DEFAULT '{}',
    notified           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, issuer_edinet_code)
);

CREATE TABLE IF NOT EXISTS edinet_issuer_map (
    issuer_edinet_code TEXT PRIMARY KEY,
    sec_code           TEXT NOT NULL DEFAULT '',
    issuer_name        TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tob_filings_date   ON tob_filings(date);
CREATE INDEX IF NOT EXISTS idx_tob_filings_issuer ON tob_filings(issuer_edinet_code);
CREATE INDEX IF NOT EXISTS idx_tob_scores_date    ON tob_scores(date);
CREATE INDEX IF NOT EXISTS idx_tob_scores_score   ON tob_scores(score DESC);

-- ── PDCA学習型バックテスト ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generation      INTEGER NOT NULL,
    experiment_type TEXT    NOT NULL,
    strategy_name   TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    params_json     TEXT    NOT NULL,
    regime          TEXT    NOT NULL DEFAULT '',
    is_daily_pnl    REAL,
    oos_daily_pnl   REAL,
    is_win_rate     REAL,
    oos_win_rate    REAL,
    is_pf           REAL,
    oos_pf          REAL,
    is_trades       INTEGER,
    oos_trades      INTEGER,
    max_dd_pct      REAL,
    score           REAL,
    robust          INTEGER NOT NULL DEFAULT 0,
    failure_reasons TEXT    NOT NULL DEFAULT '[]',
    sensitivity     REAL,
    oos_is_ratio    REAL,
    parent_exp_id   INTEGER,
    hypothesis      TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_graveyard (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    params_hash     TEXT    NOT NULL,
    failure_type    TEXT    NOT NULL,
    failure_detail  TEXT    NOT NULL DEFAULT '',
    attempts        INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(strategy_name, symbol, params_hash)
);

CREATE TABLE IF NOT EXISTS portfolio_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generation      INTEGER NOT NULL,
    combo_json      TEXT    NOT NULL,
    num_strategies  INTEGER NOT NULL,
    total_daily_pnl REAL    NOT NULL,
    portfolio_sharpe REAL,
    max_dd_pct      REAL,
    margin_util_pct REAL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS generation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generation      INTEGER NOT NULL UNIQUE,
    plan_json       TEXT    NOT NULL,
    summary         TEXT    NOT NULL DEFAULT '',
    experiments_run INTEGER NOT NULL DEFAULT 0,
    robust_found    INTEGER NOT NULL DEFAULT 0,
    best_daily_pnl  REAL,
    portfolio_pnl   REAL,
    duration_sec    REAL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exp_gen    ON experiments(generation);
CREATE INDEX IF NOT EXISTS idx_exp_sym    ON experiments(symbol);
CREATE INDEX IF NOT EXISTS idx_exp_strat  ON experiments(strategy_name);
CREATE INDEX IF NOT EXISTS idx_exp_robust ON experiments(robust);
CREATE INDEX IF NOT EXISTS idx_grave_strat ON strategy_graveyard(strategy_name, symbol);
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


def get_paper_pnl_daily(days: int = 30) -> list[dict]:
    """直近N日間のペーパートレード日別PnLを返す（daily_summaries.jp_session_pnl）。
    取引がなかった日（0円）は除外する。
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT date, jp_session_pnl AS pnl_jpy
            FROM daily_summaries
            WHERE date >= ?
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


# ── TOB監視 CRUD ─────────────────────────────────────────────────────────────

def upsert_tob_filing(f: dict) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO tob_filings
                (doc_id, date, doc_description, filer_name,
                 issuer_edinet_code, amendment_flag, parent_doc_id,
                 form_code, filing_type)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            f["doc_id"], f["date"], f.get("doc_description", ""),
            f.get("filer_name", ""), f.get("issuer_edinet_code", ""),
            f.get("amendment_flag", "0"), f.get("parent_doc_id"),
            f.get("form_code", ""), f.get("filing_type", ""),
        ))


def upsert_tob_score(s: dict) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO tob_scores
                (date, issuer_edinet_code, issuer_name, sec_code,
                 total_filings_6m, amendment_count, unique_filers,
                 has_old_amendment, pbr, market_cap_b,
                 score, score_detail, notified)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, issuer_edinet_code) DO UPDATE SET
                issuer_name=excluded.issuer_name,
                sec_code=excluded.sec_code,
                total_filings_6m=excluded.total_filings_6m,
                amendment_count=excluded.amendment_count,
                unique_filers=excluded.unique_filers,
                has_old_amendment=excluded.has_old_amendment,
                pbr=excluded.pbr,
                market_cap_b=excluded.market_cap_b,
                score=excluded.score,
                score_detail=excluded.score_detail
        """, (
            s["date"], s["issuer_edinet_code"], s.get("issuer_name", ""),
            s.get("sec_code", ""), s.get("total_filings_6m", 0),
            s.get("amendment_count", 0), s.get("unique_filers", 0),
            1 if s.get("has_old_amendment") else 0,
            s.get("pbr"), s.get("market_cap_b"),
            s.get("score", 0), json.dumps(s.get("score_detail", {}), ensure_ascii=False),
            0,
        ))


def upsert_issuer_map(edinet_code: str, sec_code: str, name: str) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO edinet_issuer_map (issuer_edinet_code, sec_code, issuer_name)
            VALUES (?,?,?)
            ON CONFLICT(issuer_edinet_code) DO UPDATE SET
                sec_code=excluded.sec_code,
                issuer_name=excluded.issuer_name,
                updated_at=datetime('now')
        """, (edinet_code, sec_code, name))


def get_issuer_map(edinet_code: str) -> dict | None:
    with _tx() as conn:
        row = conn.execute(
            "SELECT * FROM edinet_issuer_map WHERE issuer_edinet_code=?",
            (edinet_code,)
        ).fetchone()
    return dict(row) if row else None


def get_tob_filings(issuer_edinet_code: str, days: int = 180) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM tob_filings
            WHERE issuer_edinet_code=? AND date>=?
            ORDER BY date ASC
        """, (issuer_edinet_code, since)).fetchall()
    return [dict(r) for r in rows]


def get_active_issuers(days: int = 180) -> list[str]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT DISTINCT issuer_edinet_code FROM tob_filings WHERE date>=?
        """, (since,)).fetchall()
    return [r["issuer_edinet_code"] for r in rows]


def get_tob_ranking(limit: int = 30) -> list[dict]:
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM tob_scores
            WHERE date = (SELECT MAX(date) FROM tob_scores)
            ORDER BY score DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_tob_score_history(issuer_edinet_code: str, days: int = 90) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM tob_scores
            WHERE issuer_edinet_code=? AND date>=?
            ORDER BY date ASC
        """, (issuer_edinet_code, since)).fetchall()
    return [dict(r) for r in rows]


def get_unnotified_tob_scores(min_score: float = 30.0) -> list[dict]:
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM tob_scores
            WHERE date = (SELECT MAX(date) FROM tob_scores)
              AND score >= ? AND notified = 0
            ORDER BY score DESC
        """, (min_score,)).fetchall()
    return [dict(r) for r in rows]


def mark_tob_notified(date: str, issuer_edinet_code: str) -> None:
    with _tx() as conn:
        conn.execute("""
            UPDATE tob_scores SET notified=1
            WHERE date=? AND issuer_edinet_code=?
        """, (date, issuer_edinet_code))


# ── PDCA学習型バックテスト CRUD ──────────────────────────────────────────────

def save_experiment(exp: dict) -> int:
    with _tx() as conn:
        cur = conn.execute("""
            INSERT INTO experiments
                (generation, experiment_type, strategy_name, symbol, params_json,
                 regime, is_daily_pnl, oos_daily_pnl, is_win_rate, oos_win_rate,
                 is_pf, oos_pf, is_trades, oos_trades, max_dd_pct, score,
                 robust, failure_reasons, sensitivity, oos_is_ratio,
                 parent_exp_id, hypothesis)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            exp.get("generation", 0), exp.get("experiment_type", ""),
            exp.get("strategy_name", ""), exp.get("symbol", ""),
            json.dumps(exp.get("params", {}), ensure_ascii=False),
            exp.get("regime", ""),
            exp.get("is_daily_pnl"), exp.get("oos_daily_pnl"),
            exp.get("is_win_rate"), exp.get("oos_win_rate"),
            exp.get("is_pf"), exp.get("oos_pf"),
            exp.get("is_trades"), exp.get("oos_trades"),
            exp.get("max_dd_pct"), exp.get("score"),
            1 if exp.get("robust") else 0,
            json.dumps(exp.get("failure_reasons", []), ensure_ascii=False),
            exp.get("sensitivity"), exp.get("oos_is_ratio"),
            exp.get("parent_exp_id"), exp.get("hypothesis", ""),
        ))
        return cur.lastrowid


def get_robust_experiments(min_oos: float = 0, limit: int = 50) -> list[dict]:
    with _tx() as conn:
        rows = conn.execute("""
            SELECT * FROM experiments WHERE robust=1 AND oos_daily_pnl >= ?
            ORDER BY oos_daily_pnl DESC LIMIT ?
        """, (min_oos, limit)).fetchall()
    return [dict(r) for r in rows]


def get_experiment_count(strategy_name: str = "", symbol: str = "") -> int:
    with _tx() as conn:
        if strategy_name and symbol:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM experiments WHERE strategy_name=? AND symbol=?",
                (strategy_name, symbol)).fetchone()
        elif strategy_name:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM experiments WHERE strategy_name=?",
                (strategy_name,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as c FROM experiments").fetchone()
    return row["c"]


def get_latest_generation() -> int:
    with _tx() as conn:
        row = conn.execute("SELECT MAX(generation) as g FROM generation_log").fetchone()
    return row["g"] or 0


def add_to_graveyard(strategy_name: str, symbol: str, params_hash: str,
                     failure_type: str, detail: str = "") -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO strategy_graveyard
                (strategy_name, symbol, params_hash, failure_type, failure_detail)
            VALUES (?,?,?,?,?)
            ON CONFLICT(strategy_name, symbol, params_hash) DO UPDATE SET
                attempts = attempts + 1,
                failure_type = excluded.failure_type,
                failure_detail = excluded.failure_detail,
                last_attempt_at = datetime('now')
        """, (strategy_name, symbol, params_hash, failure_type, detail))


def is_in_graveyard(strategy_name: str, symbol: str, params_hash: str) -> bool:
    with _tx() as conn:
        row = conn.execute("""
            SELECT 1 FROM strategy_graveyard
            WHERE strategy_name=? AND symbol=? AND params_hash=?
        """, (strategy_name, symbol, params_hash)).fetchone()
    return row is not None


def save_generation_log(gen: int, plan_json: str, summary: str,
                        experiments_run: int, robust_found: int,
                        best_pnl: float, portfolio_pnl: float | None,
                        duration_sec: float) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO generation_log
                (generation, plan_json, summary, experiments_run, robust_found,
                 best_daily_pnl, portfolio_pnl, duration_sec)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(generation) DO UPDATE SET
                summary=excluded.summary,
                experiments_run=excluded.experiments_run,
                robust_found=excluded.robust_found,
                best_daily_pnl=excluded.best_daily_pnl,
                portfolio_pnl=excluded.portfolio_pnl,
                duration_sec=excluded.duration_sec
        """, (gen, plan_json, summary, experiments_run, robust_found,
              best_pnl, portfolio_pnl, duration_sec))


def save_portfolio_run(gen: int, combo: list[dict], total_pnl: float,
                       sharpe: float | None, max_dd: float | None,
                       margin_util: float | None) -> None:
    with _tx() as conn:
        conn.execute("""
            INSERT INTO portfolio_runs
                (generation, combo_json, num_strategies, total_daily_pnl,
                 portfolio_sharpe, max_dd_pct, margin_util_pct)
            VALUES (?,?,?,?,?,?,?)
        """, (gen, json.dumps(combo, ensure_ascii=False), len(combo),
              total_pnl, sharpe, max_dd, margin_util))


def get_untested_combos(tested_set: set | None = None, limit: int = 20) -> list[tuple[str, str]]:
    """実験ログにない (strategy_name, symbol) の組み合わせを返す。"""
    with _tx() as conn:
        rows = conn.execute("""
            SELECT DISTINCT strategy_name, symbol FROM experiments
        """).fetchall()
    if tested_set is None:
        tested_set = {(r["strategy_name"], r["symbol"]) for r in rows}
    return tested_set


def get_graveyard_hashes(strategy_name: str, symbol: str) -> set[str]:
    with _tx() as conn:
        rows = conn.execute("""
            SELECT params_hash FROM strategy_graveyard
            WHERE strategy_name=? AND symbol=?
        """, (strategy_name, symbol)).fetchall()
    return {r["params_hash"] for r in rows}


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
