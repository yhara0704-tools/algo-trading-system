"""JP株リアルタイム・ペーパートレード実行エンジン.

JPRealtimeFeed からバーを受け取り、登録済み戦略のシグナルを計算して
PaperBroker 経由でペーパー注文を出す。

セッション終了（15:30 JST）後にその日の損益をまとめて Pushover 通知する。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from backend.brokers.paper_broker import PaperBroker
from backend.capital_tier import get_tier, LIQUIDITY_MAX_POSITION
from backend.feeds.jp_realtime_feed import JPRealtimeFeed, JST, is_market_open
from backend.strategies.base import StrategyBase

# T1資金設定（松井一日信用 最低保証金）
_JP_CAPITAL_JPY: float = 300_000.0


def _live_position_scale() -> float:
    """手数料・スリップ耐性のため建玉を相対縮小（1.0=従来相当）。"""
    raw = os.getenv("JP_LIVE_POSITION_SCALE")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0.35, min(1.0, float(raw)))
        except Exception:
            pass
    prof = os.getenv("LIVE_PROFILE", "normal").strip().lower()
    return 0.88 if prof == "safe" else 1.0


_LIVE_POSITION_SCALE = _live_position_scale()
_FALLBACK_MIN_SCORE_RATIO = float(os.getenv("JP_FALLBACK_MIN_SCORE_RATIO", "0.7"))
_FALLBACK_MIN_ABS_SCORE = float(os.getenv("JP_FALLBACK_MIN_ABS_SCORE", "250.0"))
# 2026-05-02: D2 改革 — 同時保有合計が「信用枠の何 倍」を超えたら新規 entry 抑制。
# 5/1 paper で 9984.T MacdRci の高 OOS signal (期待 +8,937) を 62 件 skip した
# 構造的問題への対策。
# 既定 1.5 = 信用 99 万 × 1.5 = 148.5 万。short cluster で 4911+9984+9433 = 167 万
# まで膨らんだ 5/1 のような状況で「3 件目」 を block して余力温存する。
# (PaperBroker は短期的に cash をマイナスにできるため、broker レベルの保証金維持は
# 行わず、jp_live_runner 側で建玉価値ベースの上限を直接管理する。)
_MAX_CONCURRENT_VALUE_RATIO = float(os.getenv("JP_MAX_CONCURRENT_VALUE_RATIO", "1.5"))
# 高額ポジション (例: 9984.T 100 株 = 53 万円) の同時保有制限。
# これらが余力を一気に食い、低額銘柄の並走機会を奪うため、既定 1 つまで。
_HIGH_COST_THRESHOLD_JPY = float(os.getenv("JP_HIGH_COST_THRESHOLD_JPY", "500000"))
_HIGH_COST_MAX_CONCURRENT = int(os.getenv("JP_HIGH_COST_MAX_CONCURRENT", "1"))
_RECHECK_VOL_MIN_PCT = float(os.getenv("JP_RECHECK_VOL_MIN_PCT", "0.08"))
_RECHECK_VOL_MAX_PCT = float(os.getenv("JP_RECHECK_VOL_MAX_PCT", "1.8"))
_RECHECK_LUNCH_BLOCK = os.getenv("JP_RECHECK_LUNCH_BLOCK", "1").strip() not in {"0", "false", "False"}
# post_loss_gate 発動後、この分数を過ぎたら条件未達でも自動解除する。
# TTL を入れないと vol/regime が戻らない銘柄は終日ブロックされ、ログ・統計に雑音が残る。
_RECHECK_TTL_MIN = int(os.getenv("JP_RECHECK_TTL_MIN", "45"))
# FALLBACK WAIT の冗長なログ間引き（per symbol + reason）。
_FALLBACK_LOG_MIN_INTERVAL_SEC = int(os.getenv("JP_FALLBACK_LOG_MIN_INTERVAL_SEC", "300"))
_JP_RISK_BASE_JPY = float(os.getenv("JP_RISK_BASE_JPY", "300000"))
_JP_DAILY_LOSS_LIMIT_PCT = float(os.getenv("JP_DAILY_LOSS_LIMIT_PCT", "0.03"))
_HALT_FILE_PATH = Path(__file__).resolve().parents[2] / "data" / "jp_paper_trading_halt.json"
logger = logging.getLogger(__name__)
logger.info(
    "JP live: cost-resilience JP_LIVE_POSITION_SCALE=%.2f (env JP_LIVE_POSITION_SCALE / LIVE_PROFILE)",
    _LIVE_POSITION_SCALE,
)

_CLOSE_AFTERNOON = datetime.strptime("15:30", "%H:%M").time()

# 2026-04-22: 本日の検証で 15:00 以降に bar が飛ばない銘柄のポジションが
# `_on_bar` 由来の session_close 判定に乗らず、翌営業日に持ち越される事案を確認。
# `run()` ループ側から強制クローズするしきい値（15:05）。TSE 立会時間 15:00 + 5 分マージン。
_FORCE_CLOSE_AT_SESSION_END = datetime.strptime(
    os.getenv("JP_FORCE_CLOSE_AT", "15:05"), "%H:%M"
).time()

# 2026-04-22: エントリー後のレジーム反転（trending_up / ranging → trending_down 等）
# に対して SL 到達まで含み損を抱える前に早期撤退する仕組み。既定は有効。
_REGIME_EARLY_EXIT_ENABLED = os.getenv("JP_REGIME_EARLY_EXIT", "1").strip() not in {"0", "false", "False"}
# long 保有中に「入った時には無かった」このレジームが出たら手仕舞い候補
_ADVERSE_REGIME_LONG = {"trending_down", "high_vol"}
# short 保有中に「入った時には無かった」このレジームが出たら手仕舞い候補
_ADVERSE_REGIME_SHORT = {"trending_up", "high_vol"}
# フラップ回避: 直近 N 回連続で adverse のときだけ発火
_REGIME_ADVERSE_STREAK = int(os.getenv("JP_REGIME_ADVERSE_STREAK", "2"))
# 入った直後の誤検知を避けるためのウォームアップ
_REGIME_EARLY_EXIT_WARMUP_MIN = float(os.getenv("JP_REGIME_EARLY_EXIT_WARMUP_MIN", "1.0"))

# 2026-04-23: 本日 12:30 ジャストのロング 3 件中 2 件が 15-17 分で stop
# になった事案（全て trending_down レジーム）。後場寄り直後の
# adverse レジームでの逆張りロングを短時間だけ抑制する安全弁。
# 既定は 10 分（12:30-12:40）。shortは抑制しない。
_LUNCH_REOPEN_COOLDOWN_ENABLED = os.getenv(
    "JP_LUNCH_REOPEN_COOLDOWN", "1"
).strip() not in {"0", "false", "False"}
_LUNCH_REOPEN_COOLDOWN_MIN = int(os.getenv("JP_LUNCH_REOPEN_COOLDOWN_MIN", "10"))

# 2026-04-24: set_strategies() で paper_low_sample_excluded_latest.json を参照し、
# サンプル不足で除外された (strategy_name, symbol) 組を二重フィルタで落とす。
# 原因: 長時間稼働中のuvicornは paper_backtest_sync.py の更新後も古い module を保持し、
# 上流の collect_universe_specs で除外が走らないケースがあった（4/24 朝、6613.T 5件が該当）。
# これは file ベースのフィルタなのでデーモン長期稼働でも自動で効く。
_LOW_SAMPLE_EXCLUDED_FILE = Path(__file__).resolve().parents[2] / "data" / "paper_low_sample_excluded_latest.json"
_LOW_SAMPLE_SECOND_FILTER_ENABLED = os.getenv(
    "JP_LOW_SAMPLE_SECOND_FILTER", "1"
).strip() not in {"0", "false", "False"}

# 2026-04-24: 朝寄付 直後は市場レジーム判定のウォームアップ（_detect は len<50 で
# "unknown" を返す）が終わるまで 5m バーが数本しか溜まらず、レジーム条件を
# 踏まえない「地合い無視エントリー」が常態化していた。実績は 4/24 朝 6 件 −9,270 円。
# レジームが "unknown" の間は発注を停止し、skip_event に記録する（env で無効化可）。
# 2026-04-24（修正）: `_detect` の 50 本閾値は 5m → 250 分 = 13:10 JST となり前場を丸々
# 潰してしまう。実運用では feed は大半が 1m 足（yfinance）なので 50 本 = 50 分（9:50）
# で回復するが、5m フォールバック時は壊滅。そこで多段判定にする:
#   len < _REGIME_MIN_BARS_HARD      → "unknown"（真のウォームアップ、朝イチの一瞬だけ）
#   _REGIME_MIN_BARS_HARD <= len < 50 → 簡易判定にフォールバック（EMA20 傾き + 位置関係）
#   len >= 50                        → 通常の `market_regime._detect` をそのまま使う
# これで前場 9:00-9:20 の 20 分（=「寄り 20 分の不安定帯」）だけ guard が効き、
# それ以降は回る。Pushover の報告に「9:20 アンロック」のように書けるよう硬い閾値は env で可変。
_UNKNOWN_REGIME_GUARD_ENABLED = os.getenv(
    "JP_UNKNOWN_REGIME_GUARD", "1"
).strip() not in {"0", "false", "False"}
# フォールバック判定を「やっても意味がない」とみなす最小バー数（この未満は unknown）。
# 1 分足なら 20 本 = 9:20、5 分足なら 20 本 = 10:40（=寄り後オープニングレンジ＋α）。
_REGIME_MIN_BARS_HARD = int(os.getenv("JP_REGIME_MIN_BARS_HARD", "20"))
# フル判定に必要なバー数（`market_regime._detect` のハードコード 50 と一致）。これ未満は
# 簡易判定にフォールバックする。
_REGIME_DETECT_MIN_BARS = int(os.getenv("JP_REGIME_DETECT_MIN_BARS", "50"))

# 2026-04-27: 朝寄り直後の連敗対策。`_UNKNOWN_REGIME_GUARD` はバー数ベースの判定のため、
# yfinance/J-Quants が 9:31 時点で 1 分足を 20 本以上返してしまうと EMA20 フォールバック
# 判定が走り、unknown を抜けてしまうケースがあった（4/27 朝に 6613/4568/9433 で発生、
# 6 件 -12,700 円）。これを防ぐため、シグナル品質に関係なく時刻ベースで前場寄り直後を
# ハードロックする「朝ウォームアップ枠」を追加する。既定 30 分（09:00-09:30）で抑止。
# 0 を指定すると無効化。`JP_MORNING_WARMUP_BLOCK_MIN=0` で従来挙動。
_MORNING_WARMUP_BLOCK_MIN = max(0, int(os.getenv("JP_MORNING_WARMUP_BLOCK_MIN", "30")))

# 2026-04-27: 保有中の早期撤退に使う `_ADVERSE_REGIME_LONG/SHORT` を、
# 「新規エントリー側でも拒否する」モード。`_LUNCH_REOPEN_COOLDOWN` は時間帯ピンポイント、
# `_REGIME_EARLY_EXIT` は保有中のみ、で穴になっている「adverse 中の新規」を埋める。
# 既定 OFF（バックテストとの整合を壊さないため）。`JP_ADVERSE_REGIME_ENTRY_BLOCK=1` で有効。
_ADVERSE_REGIME_ENTRY_BLOCK = os.getenv(
    "JP_ADVERSE_REGIME_ENTRY_BLOCK", "0"
).strip() in {"1", "true", "True"}

# 2026-04-28: post_loss_recheck の regime_ok 判定を緩和するモード。
# 旧（strict）: gate.entry_regime と現在 regime が完全一致のときだけ OK。
#              entry_regime=trending_up で固定された後、現在 regime が low_vol/ranging に
#              変化すると永久に block される（4/28 6723.T で 41 件全部止められた事案）。
# 新（adverse-only）: 現在 regime が「entry_regime の真逆 (adverse)」でなければ OK。
#              long entry trending_up に対し adverse=trending_down のみ block、
#              ranging/low_vol/high_vol/trending_up は OK。
# `JP_RECHECK_REGIME_STRICT=1`（既定 1=旧挙動）を 0 に変更すると新挙動。
_RECHECK_REGIME_STRICT = os.getenv(
    "JP_RECHECK_REGIME_STRICT", "1"
).strip() in {"1", "true", "True"}
# adverse 方向辞書（baseline regime → 現在 regime がこれだったら block 継続）
_RECHECK_ADVERSE_REGIME_MAP = {
    "trending_up": "trending_down",
    "trending_down": "trending_up",
}

# 2026-04-28: sector_strength gate (C7).
# `data/sector_strength_latest.json` を起動時 + 毎時 reload で読み込み、
# weak セクター × 順張り戦略の新規エントリーを skip する。
# 段階導入のため 3 モードを用意:
#   - off (既定): gate 無効、ロード自体しない
#   - dry_log:  gate 判定を `weak_sector_long_block` で skip_event に記録するが約定は止めない (A/B 観察)
#   - on:        実際に block して return False
# `force_paper=true` の observation_pairs はバイパス。
_SECTOR_STRENGTH_MODE = os.getenv("JP_SECTOR_STRENGTH_MODE", "off").strip().lower()
if _SECTOR_STRENGTH_MODE not in {"off", "dry_log", "on"}:
    _SECTOR_STRENGTH_MODE = "off"
# 順張り系として gate 対象にする戦略名（class 名から "JP" 接頭辞を除いたもの）。
_SECTOR_STRENGTH_LONG_STRATEGIES = {"MacdRci", "Breakout", "Scalp", "EnhancedMacdRci", "EnhancedScalp"}
# sector_strength JSON のパス（毎朝 cron で再生成される想定）
_SECTOR_STRENGTH_PATH = Path(__file__).resolve().parents[2] / "data" / "sector_strength_latest.json"
# JSON reload TTL（秒）— 毎朝 cron で再生成されるので 1h で十分
_SECTOR_STRENGTH_RELOAD_SEC = max(60, int(os.getenv("JP_SECTOR_STRENGTH_RELOAD_SEC", "3600")))

# 2026-04-24: JPParabolicSwing のペーパー並走導入。
# 主足 15m + MTF 1d/1h を caller が attach する MTF 戦略のため、
# `_on_bar`（5m/1m feed）とは別系統で 15 分周期の専用ループで処理する。
# 既定は OFF（運用開始日まで静観）。有効化すると `run_parabolic_swing_loop()` が起動する。
_PARABOLIC_SWING_PAPER_ENABLED = os.getenv(
    "JP_PARABOLIC_SWING_PAPER", "0"
).strip() in {"1", "true", "True"}
# 15 分足のバー完成タイミングに合わせた tick 周期（秒）。既定 15 分。
_PARABOLIC_SWING_TICK_SEC = max(60, int(os.getenv("JP_PARABOLIC_SWING_TICK_SEC", "900")))
# 1 エントリーあたりの建玉比率（JP_MAX_POSITION_JPY に対する割合）。
_PARABOLIC_SWING_POSITION_PCT = max(
    0.05, min(0.5, float(os.getenv("JP_PARABOLIC_SWING_POSITION_PCT", "0.20")))
)
# MTF 取得範囲。PSAR / RCI / SMA5 が安定する最小本数を確保する。
_PARABOLIC_SWING_DAYS_1D = max(200, int(os.getenv("JP_PARABOLIC_SWING_DAYS_1D", "400")))
_PARABOLIC_SWING_DAYS_1H = max(30, int(os.getenv("JP_PARABOLIC_SWING_DAYS_1H", "60")))
_PARABOLIC_SWING_DAYS_15M = max(3, int(os.getenv("JP_PARABOLIC_SWING_DAYS_15M", "10")))


def _is_lunch_reopen_window(dt: datetime) -> bool:
    if not _LUNCH_REOPEN_COOLDOWN_ENABLED:
        return False
    if _LUNCH_REOPEN_COOLDOWN_MIN <= 0:
        return False
    start_m = 12 * 60 + 30
    end_m = start_m + _LUNCH_REOPEN_COOLDOWN_MIN
    hm = dt.hour * 60 + dt.minute
    return start_m <= hm < end_m


def _is_morning_warmup_window(dt: datetime) -> bool:
    """前場寄り直後の時間帯ロック (09:00 から N 分)。0 なら常に False."""
    if _MORNING_WARMUP_BLOCK_MIN <= 0:
        return False
    start_m = 9 * 60
    end_m = start_m + _MORNING_WARMUP_BLOCK_MIN
    hm = dt.hour * 60 + dt.minute
    return start_m <= hm < end_m


def _time_bucket(dt: datetime) -> str:
    hm = dt.hour * 60 + dt.minute
    if hm < 10 * 60 + 30:
        return "morning_open"
    if hm < 11 * 60 + 30:
        return "morning_late"
    if hm < 13 * 60 + 30:
        return "lunch_gap"
    if hm < 14 * 60 + 30:
        return "afternoon_early"
    return "afternoon_late"


def _get_event_tag(now: datetime) -> str:
    """当日のイベントタグを返す。"""
    try:
        from backend.backtesting.trade_guard import get_event
        return get_event(now.strftime("%Y-%m-%d")) or ""
    except Exception:
        return ""


@dataclass
class LivePosition:
    symbol: str
    strategy_id: str
    entry_price: float
    qty: int
    stop_loss: float
    take_profit: float
    entry_time: datetime
    side: str = "long"   # "long" | "short"
    entry_regime: str = "unknown"
    regime_history: list = field(default_factory=list)  # [(time, regime), ...]
    event_day: str = ""  # "SQ", "決算集中" etc. 空なら通常日


@dataclass
class LiveTrade:
    symbol: str
    strategy_id: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str   # "signal" | "stop" | "target" | "session_close"
    side: str = "long"  # "long" | "short"
    entry_regime: str = "unknown"
    exit_regime: str = "unknown"
    regime_changed: bool = False  # 保有中にレジーム変化があったか
    regime_history: list = field(default_factory=list)  # [(time_str, regime), ...]
    event_day: str = ""  # "SQ", "決算集中" etc.


@dataclass
class SubSession:
    """サブセッション — 損益ルール発動ごとに区切られる時間帯単位の記録."""
    start_time: datetime
    trades:     list[LiveTrade] = field(default_factory=list)
    end_time:   datetime | None = None
    reason:     str = ""        # "loss_limit" | "profit_target" | "session_close" | ""

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)

    def to_dict(self) -> dict:
        return {
            "start": self.start_time.strftime("%H:%M"),
            "end":   self.end_time.strftime("%H:%M") if self.end_time else "ongoing",
            "reason": self.reason,
            "pnl":   round(self.pnl, 0),
            "trades": len(self.trades),
            "win_rate": round(self.win_rate * 100, 1),
        }


@dataclass
class SessionSummary:
    date: str
    subsessions: list[SubSession] = field(default_factory=list)

    @property
    def all_trades(self) -> list[LiveTrade]:
        return [t for ss in self.subsessions for t in ss.trades]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.all_trades)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.all_trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        trades = self.all_trades
        if not trades:
            return 0.0
        return self.win_count / len(trades)

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl for t in self.all_trades if t.pnl > 0)

    @property
    def gross_loss(self) -> float:
        return sum(t.pnl for t in self.all_trades if t.pnl <= 0)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.all_trades if t.pnl > 0]
        return float(sum(wins) / len(wins)) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.all_trades if t.pnl <= 0]
        return float(sum(losses) / len(losses)) if losses else 0.0


class JPLiveRunner:
    """場中リアルタイム・ペーパートレードエンジン."""

    # サブセッション損益ルール
    # 損失上限のみ設定 — 利益上限は撤廃（良い日は前場いっぱい稼ぎ切る）
    SUBSESSION_LOSS_LIMIT_JPY:   float = -3_000.0   # -3,000円でサブセッション終了（30万資金の-1%）
    SUBSESSION_PROFIT_TARGET_JPY: float = float("inf")  # 利益上限なし
    SUBSESSION_COOLDOWN_MIN:      int   = 30         # 損失上限発動後の再開待ち（分）
    AB_TARGET_CYCLES:             int   = 3

    def __init__(self, broker: PaperBroker,
                 notify_fn: Callable | None = None) -> None:
        self._broker = broker
        self._notify = notify_fn           # async fn(title, message)
        self._feed: JPRealtimeFeed | None = None
        self._strategies: list[StrategyBase] = []
        # symbol → {strategy_id → LivePosition}
        self._positions: dict[str, dict[str, LivePosition]] = defaultdict(dict)
        self._session: SessionSummary = SessionSummary(date="")
        self._current_subsession: SubSession | None = None
        self._session_closed = False
        self._resume_after: datetime | None = None   # クールダウン解除時刻
        self._running = False
        self._experiment_tag: str = ""
        # 当日朝に記録した「バックテスト合計 OOS（円/日）」— 大引け後にペーパーが上回ったか判定
        self._session_sum_oos_snapshot: float | None = None
        self._skip_events: list[dict] = []
        # ストップ負け後の再評価ゲート（symbol単位）
        self._post_loss_gate: dict[str, dict] = {}
        self._session_start_equity: float | None = None
        # (symbol, reason) → 最終ログ時刻。FALLBACK WAIT の連呼を抑制する。
        self._fallback_wait_last_logged: dict[tuple[str, str], datetime] = {}
        # (symbol, strategy_id) → 最終「信号未成立」診断記録時刻。
        # MACD/RCI 中間値のスナップショットを一定間隔で jp_signal_skip_events に残し、
        # 未エントリー銘柄の要因（閾値未達 / セッション外 etc）を事後分析できるようにする。
        self._no_signal_diag_last: dict[tuple[str, str], datetime] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def set_paper_session_benchmark(self, sum_oos_jpy: float | None) -> None:
        """prepare_daily_strategies が計算した当日の sum(OOS日次) を保持する。"""
        self._session_sum_oos_snapshot = sum_oos_jpy

    def set_feed(self, feed: JPRealtimeFeed) -> None:
        self._feed = feed
        feed.on_bar(self._on_bar)

    def set_strategies(self, strategies: list[StrategyBase]) -> None:
        filtered = self._apply_low_sample_second_filter(strategies)
        self._strategies = filtered
        logger.info("JP live runner: %d strategies loaded", len(filtered))

    # meta.id 接頭辞 → paper_backtest_sync が扱う strategy_name のマップ。
    _ID_PREFIX_TO_STRATEGY_NAME: tuple[tuple[str, str], ...] = (
        ("jp_macd_rci_", "MacdRci"),
        ("enhanced_macd_rci_", "EnhancedMacdRci"),
        ("enhanced_scalp_", "EnhancedScalp"),
        ("jp_breakout_", "Breakout"),
        ("jp_micro_scalp_", "MicroScalp"),
        ("jp_scalp_", "Scalp"),
        ("jp_bb_short_", "BbShort"),
        ("jp_pullback_", "Pullback"),
        ("jp_swing_donchian_", "SwingDonchianD"),
        ("jp_ma_vol_", "MaVol"),
        ("jp_parabolic_swing_", "ParabolicSwing"),
    )

    @classmethod
    def _strategy_name_from_meta(cls, sid: str) -> str:
        sid = str(sid or "").strip()
        for prefix, name in cls._ID_PREFIX_TO_STRATEGY_NAME:
            if sid.startswith(prefix):
                return name
        return ""

    def _apply_low_sample_second_filter(
        self, strategies: list[StrategyBase]
    ) -> list[StrategyBase]:
        """paper_low_sample_excluded_latest.json を元に二重フィルタを適用する。

        上流の ``collect_universe_specs`` で既に除外が効いていれば素通りする。
        long-running uvicorn プロセスが古い ``paper_backtest_sync.py`` モジュールを
        保持していた 4/24 朝のようなケース（6613.T など 6 件が素通りした）でも、
        ファイルベースで最新の除外を反映できるフォールバックとして機能する。
        """
        if not _LOW_SAMPLE_SECOND_FILTER_ENABLED or not strategies:
            return list(strategies)
        if not _LOW_SAMPLE_EXCLUDED_FILE.exists():
            return list(strategies)
        try:
            raw = json.loads(_LOW_SAMPLE_EXCLUDED_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "low-sample second filter: failed to read %s: %s",
                _LOW_SAMPLE_EXCLUDED_FILE, e,
            )
            return list(strategies)
        excluded_raw = raw.get("excluded", []) if isinstance(raw, dict) else []
        if not isinstance(excluded_raw, list) or not excluded_raw:
            return list(strategies)
        exclude_pairs: set[tuple[str, str]] = set()
        for row in excluded_raw:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol", "")).strip()
            sname = str(row.get("strategy_name", "")).strip()
            if sym and sname:
                exclude_pairs.add((sname, sym))
        if not exclude_pairs:
            return list(strategies)

        kept: list[StrategyBase] = []
        dropped: list[tuple[str, str]] = []
        for s in strategies:
            sym = str(getattr(s.meta, "symbol", "") or "").strip()
            sid = str(getattr(s.meta, "id", "") or "").strip()
            strategy_name = self._strategy_name_from_meta(sid)
            if strategy_name and (strategy_name, sym) in exclude_pairs:
                dropped.append((sym, strategy_name))
                continue
            kept.append(s)
        if dropped:
            logger.warning(
                "low-sample second filter dropped %d strategies: %s",
                len(dropped),
                ", ".join(f"{sym}[{sn}]" for sym, sn in dropped),
            )
        return kept

    def _reset_broker_daily(self, today: str) -> None:
        """日付切替時に PaperBroker の現金・ポジションを初期化する。

        LiveRunner 未追跡のゴーストポジション（コールバック例外で `_positions`
        登録が抜けた買い）が翌営業日に持ち越されると、現金 100 万円近くが塞がれ
        `insufficient_lot` を量産する。ペーパー運用は日計り前提なので、日次で
        `starting_cash` に戻して不可視ポジションを一掃する。
        """
        try:
            before_cash = float(self._broker.get_account().cash)
            before_equity = float(self._broker.get_account().equity)
            positions_before = list(self._broker.get_positions())
            tracked_keys = {(s, sid) for s, by in self._positions.items() for sid in by.keys()}
            ghosts = [p for p in positions_before if not any(p.symbol == s for s, _ in tracked_keys)]
            starting = float(getattr(self._broker, "_starting_cash", before_cash))
            # PaperBroker は状態を private dict で持つため直接差し替える
            self._broker._cash = starting            # type: ignore[attr-defined]
            self._broker._positions = {}             # type: ignore[attr-defined]
            if ghosts or positions_before:
                logger.warning(
                    "Broker daily reset [%s]: cash %.0f→%.0f, cleared %d positions (ghosts=%d). tracked=%d",
                    today, before_cash, starting, len(positions_before), len(ghosts),
                    len(tracked_keys),
                )
            else:
                logger.info(
                    "Broker daily reset [%s]: cash %.0f→%.0f equity_before=%.0f",
                    today, before_cash, starting, before_equity,
                )
        except Exception as e:
            logger.warning("Broker daily reset failed: %s", e)

    async def _sell_ghosts(self, ghost_symbols: list[str], *, reason: str) -> None:
        """ランナー未追跡（ゴースト）ポジションを売却して cash を回復する。

        `_on_jp_paper_fill` 修正後はほぼ発生しない想定だが、念のための保険。
        単独再発でも運用が止まらないよう、エラーは握りつぶしてログだけ残す。
        """
        try:
            broker_positions = list(self._broker.get_positions())
        except Exception as e:
            logger.warning("ghost sell: get_positions failed: %s", e)
            return
        sold = 0
        for p in broker_positions:
            if p.symbol not in ghost_symbols:
                continue
            try:
                price = float(getattr(p, "avg_price", 0.0) or 0.0)
                if self._feed:
                    try:
                        bars = self._feed.get_bars(p.symbol)
                        if bars is not None and len(bars) >= 1:
                            price = float(bars["close"].iloc[-1])
                    except Exception:
                        pass
                if price <= 0:
                    continue
                order = await self._broker.place_order(p.symbol, "sell", float(p.qty), price)
                if order.status == "filled":
                    sold += 1
                    logger.warning("ghost sold (%s): %s qty=%d @%.1f", reason, p.symbol, p.qty, price)
            except Exception as e:
                logger.warning("ghost sell failed [%s]: %s", p.symbol, e)
        if sold:
            try:
                new_cash = float(self._broker.get_account().cash)
                logger.warning("ghost recovery: sold=%d → cash=%.0f", sold, new_cash)
            except Exception:
                pass

    async def _force_close_open_positions(self, now: datetime) -> None:
        """セッション終了時（15:05+）に残っている未決済ポジションを強制クローズ。

        `_on_bar` 内の session_close 判定は bar 到達に依存するため、15:00 以降 feed が
        新 bar を届けない銘柄は取り残される。このメソッドは `run()` ループから呼ばれる
        ため、bar の有無に関わらず必ず全ポジションを消す。

        対象:
            1. `self._positions` に登録済みのランナー管理ポジション → `_close_position` 経由で DB まで記録
            2. ブローカーにしか存在しないゴースト → 直接 sell し警告ログのみ（履歴には残せない）
        """
        if self._session_closed:
            return

        closures = 0
        errors: list[str] = []
        for sym, by_sid in list(self._positions.items()):
            for sid, pos in list(by_sid.items()):
                try:
                    last_price = float(pos.entry_price)
                    if self._feed:
                        try:
                            bars = self._feed.get_bars(sym)
                            if bars is not None and len(bars) >= 1:
                                last_price = float(bars["close"].iloc[-1])
                        except Exception:
                            pass
                    await self._close_position(pos, last_price, "session_close_forced", now)
                    del self._positions[sym][sid]
                    closures += 1
                except Exception as e:
                    errors.append(f"{sym}/{sid}: {e}")

        # ブローカーにだけ残っているゴースト（_positions に載っていない qty>0）は
        # 記録不能なので最終手段として素売りし警告だけ残す。持ち越し＝翌日の現金拘束を防ぐ。
        ghost_cleared = 0
        try:
            broker_positions = list(self._broker.get_positions())
            tracked = {(s, sid) for s, by in self._positions.items() for sid in by.keys()}
            for p in broker_positions:
                if any(p.symbol == s for s, _ in tracked):
                    continue
                try:
                    px = float(getattr(p, "avg_price", 0.0) or 0.0)
                    if self._feed:
                        try:
                            bars = self._feed.get_bars(p.symbol)
                            if bars is not None and len(bars) >= 1:
                                px = float(bars["close"].iloc[-1])
                        except Exception:
                            pass
                    if px <= 0:
                        continue
                    order = await self._broker.place_order(p.symbol, "sell", float(p.qty), px)
                    if order.status == "filled":
                        logger.warning(
                            "EOD ghost sold: %s qty=%d @%.1f (untracked by runner)",
                            p.symbol, p.qty, px,
                        )
                        ghost_cleared += 1
                except Exception as e:
                    logger.warning("EOD ghost cleanup failed [%s]: %s", p.symbol, e)
        except Exception as e:
            logger.warning("EOD ghost scan failed: %s", e)

        if closures or ghost_cleared or errors:
            logger.warning(
                "Force-close at session end: tracked_closed=%d ghost_sold=%d errors=%d [%s]",
                closures, ghost_cleared, len(errors),
                "; ".join(errors[:3]) if errors else "-",
            )

    async def run_scalp_loop(self) -> None:
        """1分足スキャルピング専用ループ。
        毎分yfinanceから最新1分足バーを取得してシグナルを生成・実行する。
        市場時間(9:00-15:30)のみ動作。手数料ゼロ前提(日計り信用)。
        """
        import asyncio
        logger.info("1分足スキャルループ 開始")
        while self._running:
            now = datetime.now(JST)

            # 市場時間外はスキップ
            if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)):
                await asyncio.sleep(60)
                continue

            # 1分足バーを取得して各戦略に流す
            symbols = list({s.meta.symbol for s in self._strategies
                           if s.meta.interval == "1m"})
            for sym in symbols:
                try:
                    df = await self._fetch_1min_bars(sym)
                    if df is not None and len(df) >= 20:
                        await self._on_bar(sym, df)
                except Exception as e:
                    logger.debug("Scalp loop error [%s]: %s", sym, e)

            # 次の分の頭まで待機（オーバーシュート防止）
            wait = 60 - datetime.now(JST).second
            await asyncio.sleep(max(wait, 5))

    async def _fetch_1min_bars(self, symbol: str) -> pd.DataFrame | None:
        """当日の店内足（yfinance→J-Quants 5m と jp_realtime_feed.fetch_intraday と同一）。"""
        import asyncio

        from backend.feeds.jp_realtime_feed import fetch_intraday

        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: fetch_intraday(symbol)),
                timeout=28,
            )
        except Exception:
            return None

    # ── ParabolicSwing（MTF 15m/1h/1d）ペーパー並走ループ ─────────────────────
    # 主足 15m で PSAR 反転、1d / 1h を caller 側で attach する設計のため、
    # 5m feed ベースの `_on_bar` とは別に 15 分周期で回す専用ループを用意する。
    # `_strategies` から prefix `jp_parabolic_swing_` の戦略だけ抽出して処理し、
    # 既存の broker / `_positions` / SessionSummary をそのまま共有する
    # （DB 書き込み / 15:05 強制クローズ / low-sample 二重フィルタが自動で効く）。

    @classmethod
    def _is_parabolic_swing_strategy(cls, strategy: StrategyBase) -> bool:
        sid = str(getattr(getattr(strategy, "meta", None), "id", "") or "")
        return sid.startswith("jp_parabolic_swing_")

    def _parabolic_swing_strategies(self) -> list[StrategyBase]:
        return [s for s in self._strategies if self._is_parabolic_swing_strategy(s)]

    async def _fetch_parabolic_swing_mtf(
        self, symbol: str
    ) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
        """(df_15m, df_h1, df_d) を取得する。いずれかが空なら None を返す。

        ``backend.lab.runner.fetch_ohlcv`` を利用するのでメモリ + ファイル
        キャッシュ + J-Quants / yfinance のフォールバックが効く。場中は 5 分 TTL。
        """
        from backend.lab.runner import fetch_ohlcv

        async def _safe(interval: str, days: int) -> pd.DataFrame | None:
            try:
                df = await asyncio.wait_for(fetch_ohlcv(symbol, interval, days), timeout=45)
                return df if df is not None and not df.empty else None
            except Exception as e:
                logger.warning(
                    "parabolic swing fetch %s/%s failed: %s", symbol, interval, e
                )
                return None

        df_15m = await _safe("15m", _PARABOLIC_SWING_DAYS_15M)
        df_h1 = await _safe("1h", _PARABOLIC_SWING_DAYS_1H)
        df_d = await _safe("1d", _PARABOLIC_SWING_DAYS_1D)
        return df_15m, df_h1, df_d

    async def _try_open_swing_position(
        self,
        strategy: StrategyBase,
        symbol: str,
        df_15m: pd.DataFrame,
        latest_price: float,
        stop_loss: float,
        now: datetime,
    ) -> tuple[bool, str]:
        """ParabolicSwing 用のエントリー処理。

        scalping 専用のガード（post-loss gate / 昼寄コールダウン等）は適用せず、
        「unknown レジーム / manual halt / 日次損失ガード / 資金不足」のみチェック。
        サイジングは ``JP_PARABOLIC_SWING_POSITION_PCT`` で固定、trailing PSAR を
        ``stop_loss`` に持ち、take_profit は動的 exit（signal=-1）に委ねる。
        """
        sid = strategy.meta.id
        halt_info = self._load_halt_file()
        if halt_info.get("halt", False):
            reason = str(halt_info.get("reason", "manual_halt"))
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="manual_halt",
                edge_score=0.0,
                detail={"halt_reason": reason, "kind": "parabolic_swing"},
            )
            return False, "manual_halt"

        # unknown レジームの間は発注しない（朝イチ warmup / データ不足対策）。
        # 15m 足なら `_REGIME_MIN_BARS_HARD=20` 本 = 5 時間分 ≒ 前日までのデータで
        # 既に満たされているのが通常。9:00 時点で attach 済み MTF を使う Swing 戦略は
        # 初日から 15m データが充分にあるため、ほぼ unknown にならない想定。
        entry_regime = self._detect_entry_regime(symbol, df_15m)
        if _UNKNOWN_REGIME_GUARD_ENABLED and entry_regime == "unknown":
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="unknown_regime_guard",
                edge_score=0.0,
                detail={
                    "bars": int(len(df_15m)) if df_15m is not None else 0,
                    "min_bars_hard": _REGIME_MIN_BARS_HARD,
                    "min_bars_full": _REGIME_DETECT_MIN_BARS,
                    "kind": "parabolic_swing",
                },
            )
            logger.info(
                "SWING ENTRY BLOCKED (unknown regime): %s [%s]",
                symbol, sid,
            )
            return False, "unknown_regime_guard"

        risk_ok, risk_detail = self._check_daily_loss_guard(now, symbol, latest_price)
        if not risk_ok:
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="daily_loss_guard",
                edge_score=0.0,
                detail={**risk_detail, "kind": "parabolic_swing"},
            )
            return False, "daily_loss_guard"

        position_value = _JP_CAPITAL_JPY * _PARABOLIC_SWING_POSITION_PCT * _LIVE_POSITION_SCALE
        liq_cap = LIQUIDITY_MAX_POSITION.get(symbol)
        if liq_cap is not None:
            position_value = min(position_value, liq_cap)
        try:
            cash = float(self._broker.get_account().cash)
            position_value = min(position_value, max(0.0, cash * 0.98))
        except Exception:
            cash = 0.0
        qty = int(position_value / latest_price / 100) * 100 if latest_price > 0 else 0
        if qty < 100:
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="insufficient_lot",
                edge_score=0.0,
                detail={
                    "cash": cash,
                    "position_value": position_value,
                    "latest_price": latest_price,
                    "kind": "parabolic_swing",
                },
            )
            return False, "insufficient_lot"

        order = await self._broker.place_order(symbol, "buy", float(qty), latest_price)
        if order.status != "filled":
            logger.warning(
                "SWING order rejected [%s/%s]: %s", symbol, sid, getattr(order, "note", "")
            )
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="order_rejected",
                edge_score=0.0,
                detail={"note": str(getattr(order, "note", "")), "kind": "parabolic_swing"},
            )
            return False, "order_rejected"

        # trailing PSAR（generate_signals の `stop_loss` 列）は _process で毎 tick
        # 更新する。初期値はエントリ時の PSAR をそのまま採用する。
        safe_sl = float(stop_loss) if stop_loss == stop_loss else latest_price * 0.97
        self._positions[symbol][sid] = LivePosition(
            symbol=symbol,
            strategy_id=sid,
            entry_price=latest_price,
            qty=qty,
            stop_loss=safe_sl,
            take_profit=float("nan"),
            entry_time=now,
            side="long",
            entry_regime=entry_regime,
            regime_history=[(now.strftime("%H:%M"), entry_regime)],
            event_day=_get_event_tag(now),
        )
        logger.info(
            "SWING ENTRY %s qty=%d @%.1f SL=%.1f regime=%s [%s]",
            symbol, qty, latest_price, safe_sl, entry_regime, sid,
        )
        return True, "ok"

    async def _process_parabolic_swing_strategy(
        self, strategy: StrategyBase, now: datetime
    ) -> None:
        """1 戦略分の MTF fetch → attach → signal 判定 → entry/exit 処理。

        例外は各戦略ごとに捕捉してループ全体を止めない。
        """
        symbol = strategy.meta.symbol
        sid = strategy.meta.id
        try:
            df_15m, df_h1, df_d = await self._fetch_parabolic_swing_mtf(symbol)
            if df_15m is None or df_h1 is None or df_d is None:
                logger.debug(
                    "SWING mtf data missing %s (15m=%s h1=%s d=%s)",
                    symbol,
                    None if df_15m is None else len(df_15m),
                    None if df_h1 is None else len(df_h1),
                    None if df_d is None else len(df_d),
                )
                return
            # attach は副作用メソッドなので毎 tick やり直して問題ない
            try:
                strategy.attach(df_d=df_d, df_h1=df_h1)  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning("SWING attach failed [%s]: %s", sid, e)
                return
            try:
                signals = strategy.generate_signals(df_15m)
            except Exception as e:
                logger.warning("SWING generate_signals failed [%s]: %s", sid, e)
                return
            if signals is None or signals.empty:
                return
            last = signals.iloc[-1]
            last_sig = int(last.get("signal", 0) or 0)
            last_close = float(last.get("close", df_15m["close"].iloc[-1]))
            last_stop = last.get("stop_loss", float("nan"))
            try:
                last_stop_f = float(last_stop)
            except Exception:
                last_stop_f = float("nan")

            # すでにポジションがあるか？
            pos = self._positions.get(symbol, {}).get(sid)
            if pos is not None:
                # trailing stop update（上方向のみ）。戦略の generate_signals 内では
                # 「signal==1 の bar にしか stop_loss が入らない」ので、trailing 用の
                # PSAR を追加で参照する。15m PSAR を毎足更新の stop として使うモード。
                if (
                    getattr(strategy, "sl_mode", "psar_15m") == "psar_15m"
                    and "psar_15m" in signals.columns
                ):
                    try:
                        psar_now = float(signals["psar_15m"].iloc[-1])
                        if psar_now == psar_now and psar_now > pos.stop_loss and psar_now < last_close:
                            old_sl = pos.stop_loss
                            pos.stop_loss = psar_now
                            logger.info(
                                "SWING trailing SL update %s: %.1f → %.1f (close=%.1f) [%s]",
                                symbol, old_sl, psar_now, last_close, sid,
                            )
                    except Exception:
                        pass

                # stop hit → close
                if pos.stop_loss == pos.stop_loss and last_close <= pos.stop_loss:
                    await self._close_position(pos, last_close, "stop", now)
                    try:
                        del self._positions[symbol][sid]
                    except KeyError:
                        pass
                    return
                # signal=-1 → close (動的利確)
                if last_sig == -1:
                    await self._close_position(pos, last_close, "signal", now)
                    try:
                        del self._positions[symbol][sid]
                    except KeyError:
                        pass
                    return
                # otherwise hold
                return

            # ポジション無し → signal==1 で open
            if last_sig == 1:
                await self._try_open_swing_position(
                    strategy=strategy,
                    symbol=symbol,
                    df_15m=df_15m,
                    latest_price=last_close,
                    stop_loss=last_stop_f,
                    now=now,
                )
        except Exception as e:
            logger.warning("SWING process failed [%s/%s]: %s", symbol, sid, e)

    async def run_parabolic_swing_loop(self) -> None:
        """JPParabolicSwing 戦略のペーパー並走ループ（15 分周期）。

        `JP_PARABOLIC_SWING_PAPER=1` で起動。無効時は即終了する。
        市場時間内のみ tick 処理を行い、場外では tick 周期分スリープする。
        """
        if not _PARABOLIC_SWING_PAPER_ENABLED:
            logger.info(
                "ParabolicSwing paper loop disabled (JP_PARABOLIC_SWING_PAPER=0)"
            )
            return
        logger.info(
            "ParabolicSwing paper loop started: tick=%ds pos_pct=%.2f (days 1d/1h/15m=%d/%d/%d)",
            _PARABOLIC_SWING_TICK_SEC,
            _PARABOLIC_SWING_POSITION_PCT,
            _PARABOLIC_SWING_DAYS_1D,
            _PARABOLIC_SWING_DAYS_1H,
            _PARABOLIC_SWING_DAYS_15M,
        )
        await asyncio.sleep(90)  # feed / strategies の初期化を待つ
        while self._running:
            try:
                now = datetime.now(JST)
                if not is_market_open():
                    await asyncio.sleep(_PARABOLIC_SWING_TICK_SEC)
                    continue
                strategies = self._parabolic_swing_strategies()
                if not strategies:
                    await asyncio.sleep(_PARABOLIC_SWING_TICK_SEC)
                    continue
                logger.debug(
                    "SWING tick %s: %d strategies",
                    now.strftime("%H:%M"), len(strategies),
                )
                for strat in strategies:
                    if not self._running:
                        break
                    await self._process_parabolic_swing_strategy(strat, now)
                await asyncio.sleep(_PARABOLIC_SWING_TICK_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("ParabolicSwing loop error: %s", e)
                await asyncio.sleep(60)

    def get_session(self) -> dict:
        s = self._session
        in_cooldown = (self._resume_after is not None
                       and datetime.now(JST) < self._resume_after)
        resume_at = (self._resume_after.strftime("%H:%M")
                     if self._resume_after else None)
        return {
            "date":           s.date,
            "experiment_tag": self._experiment_tag,
            "total_pnl":      s.total_pnl,
            "gross_profit":   s.gross_profit,
            "gross_loss":     s.gross_loss,
            "avg_win":        s.avg_win,
            "avg_loss":       s.avg_loss,
            "num_trades":     len(s.all_trades),
            "win_rate":       s.win_rate,
            "in_cooldown":    in_cooldown,
            "resume_at":      resume_at,
            "subsessions":    [ss.to_dict() for ss in s.subsessions],
            "current_subsession": (
                self._current_subsession.to_dict()
                if self._current_subsession else None
            ),
            "trades": [
                {
                    "symbol":   t.symbol,
                    "strategy": t.strategy_id,
                    "side":     t.side,
                    "entry":    t.entry_price,
                    "exit":     t.exit_price,
                    "qty":      t.qty,
                    "pnl":      round(t.pnl, 0),
                    "reason":   t.exit_reason,
                }
                for t in s.all_trades
            ],
        }

    async def run(self) -> None:
        """セッション監視ループ。市場が開くたびにセッションをリセットし、
        15:30 JST 過ぎにセッションサマリーを送信する。"""
        self._running = True
        last_date: str = ""
        logger.info("JPLiveRunner started.")

        while self._running:
            now = datetime.now(JST)
            today = now.strftime("%Y-%m-%d")

            # 日付が変わったらセッションリセット
            if today != last_date:
                self._session            = SessionSummary(date=today)
                self._current_subsession = SubSession(start_time=now)
                self._resume_after       = None
                self._session_closed     = False
                self._experiment_tag     = self._resolve_experiment_tag(today)
                self._skip_events        = []
                self._post_loss_gate     = {}
                self._fallback_wait_last_logged = {}
                # ランナー側で把握していないブローカー内ポジションは「ゴースト」。
                # コールバック例外などで ENTRY ログと _positions 登録が抜け落ちると、
                # 買い付け現金だけが差し引かれてゴーストが積み上がり、
                # 翌営業日の T1 建玉が軒並み insufficient_lot になる。
                # → 日付切替タイミングで broker を starting_cash 起点に揃える。
                self._reset_broker_daily(today)
                try:
                    self._session_start_equity = float(self._broker.get_account().equity)
                except Exception:
                    self._session_start_equity = _JP_RISK_BASE_JPY
                last_date = today
                logger.info("New session: %s (exp=%s)", today, self._experiment_tag or "-")

            # クールダウン解除チェック → 新サブセッション開始
            if self._resume_after and now >= self._resume_after:
                logger.info("Subsession cooldown ended. Resuming at %s", now.strftime("%H:%M"))
                self._resume_after       = None
                self._current_subsession = SubSession(start_time=now)

            # 2026-04-22: 15:05 以降に未決済ポジションが残っていたら強制クローズする。
            # `_on_bar` 内の session_close 判定は「bar 到達」に依存しており、15:00 以降に
            # feed が新 bar を届けない銘柄が取り残されるケースが確認された（翌営業日に
            # 含み損益が持ち越され、日次サマリーの数値とも食い違う）。
            if (not self._session_closed
                    and now.time() >= _FORCE_CLOSE_AT_SESSION_END):
                await self._force_close_open_positions(now)

            # 15:30以降で未送信ならサブセッションを確定後にサマリー送信
            if (not self._session_closed
                    and now.time() >= _CLOSE_AFTERNOON):
                # 先に最後のサブセッションをクローズして all_trades 判定に反映する
                if self._current_subsession and self._current_subsession.trades:
                    self._current_subsession.end_time = now
                    self._current_subsession.reason = "session_close"
                    self._session.subsessions.append(self._current_subsession)
                    self._current_subsession = None

                await self._send_session_summary()
                self._session_closed = True

            await asyncio.sleep(30)

    async def stop(self) -> None:
        self._running = False

    # ── Bar callback ──────────────────────────────────────────────────────────

    async def _on_bar(self, symbol: str, df: pd.DataFrame) -> None:
        """JPRealtimeFeed から新しいバーが届いたときに呼ばれる。"""
        if df.empty or len(df) < 2:
            return

        now = datetime.now(JST)
        latest = df.iloc[-1]
        latest_price: float = float(latest["close"])

        for strategy in self._strategies:
            if strategy.meta.symbol != symbol:
                continue
            try:
                await self._process_strategy(strategy, symbol, df, latest_price, now)
            except Exception as e:
                logger.error("LiveRunner strategy error [%s/%s]: %s",
                             symbol, strategy.meta.id, e)

    async def _process_strategy(
        self,
        strategy: StrategyBase,
        symbol: str,
        df: pd.DataFrame,
        latest_price: float,
        now: datetime,
    ) -> None:
        sid = strategy.meta.id
        pos = self._positions[symbol].get(sid)

        # A/B実験のエントリー制御（既存ポジション管理は常に継続）
        if pos is None:
            gate_ok, gate_detail = self._evaluate_post_loss_gate(symbol, sid, df, now)
            if not gate_ok:
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="post_loss_recheck_block",
                    edge_score=self._expected_edge_score(strategy),
                    detail=gate_detail,
                )
                gate_state = self._post_loss_gate.get(symbol, {})
                if not gate_state.get("rotation_attempted", False):
                    gate_state["rotation_attempted"] = True
                    self._post_loss_gate[symbol] = gate_state
                    await self._try_fallback_entry(
                        blocked_strategy=strategy,
                        blocked_reason="post_loss_recheck_block",
                        now=now,
                    )
                return
            if self._experiment_tag == "A":
                # 案A: 負け上位2手法（MACD×RCI Unitika / QD Laser）を停止
                is_macd_rci = ("macd_rci" in sid.lower()) or ("macd" in sid.lower() and "rci" in sid.lower())
                if is_macd_rci and symbol in {"3103.T", "6613.T"}:
                    return
            elif self._experiment_tag == "B":
                # 案B: 09:00-09:30 の新規エントリーを停止
                if now.time() < datetime.strptime("09:30", "%H:%M").time():
                    return

        # ── ポジションあり: SL/TP/セッションクローズチェック ──────────────────
        if pos:
            # 保有中レジーム追跡（バーごとに最新レジームを記録）
            try:
                if df is not None and len(df) >= 30:
                    from backend.market_regime import _detect as _det
                    current_regime = _det(symbol, df).regime
                    last_recorded = pos.regime_history[-1][1] if pos.regime_history else "unknown"
                    if current_regime != last_recorded:
                        pos.regime_history.append((now.strftime("%H:%M"), current_regime))
                        logger.info("REGIME CHANGE %s [%s]: %s → %s (保有中)",
                                    symbol, sid, last_recorded, current_regime)
            except Exception:
                pass

            exit_reason: str | None = None

            if pos.side == "long":
                if latest_price <= pos.stop_loss:
                    exit_reason = "stop"
                elif latest_price >= pos.take_profit:
                    exit_reason = "target"
            else:  # short: SLは上、TPは下
                if latest_price >= pos.stop_loss:
                    exit_reason = "stop"
                elif latest_price <= pos.take_profit:
                    exit_reason = "target"

            # 2026-04-22: エントリー後のレジーム反転で刈られ続ける問題への対処。
            # エントリー時が bullish-like / neutral で、保有中に直近 N 回連続で
            # adverse レジームになったら SL 到達前に撤退する（含み損は即時確定）。
            if exit_reason is None and _REGIME_EARLY_EXIT_ENABLED:
                hist_regimes = [r for _, r in pos.regime_history]
                if len(hist_regimes) >= _REGIME_ADVERSE_STREAK:
                    adverse = (
                        _ADVERSE_REGIME_LONG if pos.side == "long"
                        else _ADVERSE_REGIME_SHORT
                    )
                    tail = hist_regimes[-_REGIME_ADVERSE_STREAK:]
                    mins_since_entry = (now - pos.entry_time).total_seconds() / 60.0
                    entry_was_ok = pos.entry_regime not in adverse
                    if (
                        entry_was_ok
                        and mins_since_entry >= _REGIME_EARLY_EXIT_WARMUP_MIN
                        and all(r in adverse for r in tail)
                    ):
                        exit_reason = "regime_flip"
                        cur_pnl = (
                            (latest_price - pos.entry_price) if pos.side == "long"
                            else (pos.entry_price - latest_price)
                        ) * pos.qty
                        logger.info(
                            "REGIME FLIP EXIT %s [%s]: entry=%s → now=%s "
                            "(hold=%.0fm pnl=%+.0f streak=%d)",
                            symbol, sid, pos.entry_regime, tail[-1],
                            mins_since_entry, cur_pnl, _REGIME_ADVERSE_STREAK,
                        )

            if exit_reason is None and now.time() >= _CLOSE_AFTERNOON:
                exit_reason = "session_close"

            # 同一セクター異変検知 → 緊急撤退（決算銘柄は除外）
            if exit_reason is None:
                try:
                    from backend.backtesting.trade_guard import (
                        get_sector_peers, detect_peer_anomaly, is_earnings_day_sync,
                    )
                    peers = get_sector_peers(symbol)
                    if peers and self._feed:
                        today_str = now.strftime("%Y-%m-%d")
                        # 決算銘柄を除外（個社要因の急落でセクター異変と誤判定しない）
                        earnings_exclude = {
                            p for p in peers
                            if is_earnings_day_sync(p, today_str)
                        }
                        peer_prices = {}
                        for p in peers:
                            p_bars = self._feed.get_bars(p)
                            if p_bars is not None and len(p_bars) >= 5:
                                peer_prices[p] = p_bars["close"].tail(5).tolist()
                        anomalies = detect_peer_anomaly(
                            peer_prices, threshold_pct=-1.5,
                            exclude_symbols=earnings_exclude,
                        )
                        if anomalies:
                            exit_reason = "peer_anomaly"
                            logger.warning("PEER ANOMALY %s → %s 緊急撤退 (決算除外: %s)",
                                           symbol, anomalies, earnings_exclude or "なし")
                except Exception:
                    pass

            if exit_reason:
                await self._close_position(pos, latest_price, exit_reason, now)
                if exit_reason == "stop":
                    self._set_post_loss_gate(symbol, sid, pos, df, now)
                del self._positions[symbol][sid]
            return

        # ── ポジションなし: シグナル確認 ─────────────────────────────────────
        # セッション終了30分前は新規エントリー禁止
        close_dt = now.replace(hour=15, minute=0, second=0, microsecond=0)
        if now >= close_dt:
            return

        # クールダウン中はエントリーしない（ただし既存ポジションの管理は続ける）
        if self._resume_after and now < self._resume_after:
            return

        # イベント日フラグ（ペーパーではエントリーしてデータ取得、実弾時は別途制御）
        # → データ蓄積のためペーパートレードではブロックしない

        # サブセッション損益ルールチェック
        ss = self._current_subsession
        if ss is not None:
            ss_pnl = ss.pnl
            reason: str | None = None
            if ss_pnl <= self.SUBSESSION_LOSS_LIMIT_JPY:
                reason = "loss_limit"
                msg    = f"⛔ 損失上限到達 ({ss_pnl:+,.0f}円)  {self.SUBSESSION_COOLDOWN_MIN}分クールダウン後に再開"
            else:
                reason = None   # 利益上限なし — 稼げる日は稼ぎ切る

            if reason:
                # サブセッションを閉じてクールダウン開始
                ss.end_time = now
                ss.reason   = reason
                self._session.subsessions.append(ss)
                self._current_subsession = None
                self._resume_after = now + timedelta(minutes=self.SUBSESSION_COOLDOWN_MIN)
                logger.info("Subsession closed [%s] pnl=%+.0f  resume@%s",
                            reason, ss_pnl, self._resume_after.strftime("%H:%M"))
                if self._notify:
                    asyncio.ensure_future(
                        self._notify(
                            "サブセッション終了",
                            msg,
                            category="jp_paper",
                            source="jp_live_runner._process_strategy",
                        )
                    )
                return

        # サブセッションがなければ開始
        if self._current_subsession is None:
            self._current_subsession = SubSession(start_time=now)

        try:
            signals = strategy.generate_signals(df)
        except Exception as e:
            logger.warning("Signal generation error [%s]: %s", sid, e)
            return

        last_signal = signals["signal"].iloc[-1]
        if last_signal not in (1, -2):
            # 信号未成立時に MACD/RCI の中間値を定期スナップショット。
            # 「no_signal（未達）」の内訳（MACD が 0 割れ / RCI 多数決未達 / セッション外 等）を
            # jp_signal_skip_events に残し、後日の要因分析に回す。
            self._record_no_signal_diag(symbol, sid, signals, now)
            return

        is_long = (last_signal == 1)
        last_row = signals.iloc[-1]

        if is_long:
            sl = float(last_row.get("stop_loss", latest_price * 0.995))
            tp = float(last_row.get("take_profit", latest_price * 1.01))
            if pd.isna(sl): sl = latest_price * 0.995
            if pd.isna(tp): tp = latest_price * 1.01
        else:  # short
            sl = float(last_row.get("stop_loss", latest_price * 1.005))
            tp = float(last_row.get("take_profit", latest_price * 0.99))
            if pd.isna(sl): sl = latest_price * 1.005
            if pd.isna(tp): tp = latest_price * 0.99

        if is_long and _is_lunch_reopen_window(now):
            regime_now = "unknown"
            try:
                if df is not None and len(df) >= 30:
                    from backend.market_regime import _detect as _det
                    regime_now = _det(symbol, df).regime
            except Exception:
                regime_now = "unknown"
            if regime_now in _ADVERSE_REGIME_LONG:
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="lunch_reopen_cooldown",
                    edge_score=self._expected_edge_score(strategy),
                    detail={
                        "regime": regime_now,
                        "window_min": _LUNCH_REOPEN_COOLDOWN_MIN,
                        "side": "long",
                    },
                )
                logger.info(
                    "ENTRY BLOCKED (lunch_reopen_cooldown): %s [%s] regime=%s window=%dmin",
                    symbol, sid, regime_now, _LUNCH_REOPEN_COOLDOWN_MIN,
                )
                return

        entered, fail_reason = await self._try_open_position(
            strategy=strategy,
            symbol=symbol,
            df=df,
            latest_price=latest_price,
            now=now,
            is_long=is_long,
            stop_loss=sl,
            take_profit=tp,
        )
        if entered:
            return
        if fail_reason in {"insufficient_lot", "insufficient_cash"}:
            await self._try_fallback_entry(
                blocked_strategy=strategy,
                blocked_reason=fail_reason,
                now=now,
            )

    def _detect_entry_regime(self, symbol: str, df: pd.DataFrame) -> str:
        """発注前のレジーム判定（多段）。

        ``market_regime._detect`` は内部に ADX14 / ATR14 のハードコード閾値
        (``len<50`` → "unknown") を持つため、5 分足では 250 分 = 13:10 JST まで
        封じる。これは前場を丸々潰すので、以下の多段判定に変更した:

          - ``len < _REGIME_MIN_BARS_HARD``          → "unknown"（真のウォームアップ）
          - ``_REGIME_MIN_BARS_HARD <= len < 50``    → 簡易判定へフォールバック
          - ``len >= 50``                            → 通常 `_detect` を使用

        簡易判定は ATR / ADX を使えない短いバーでも使えるように EMA20 の傾きと
        最新 close の位置関係だけで trending_up / trending_down / ranging を返す。
        """
        if df is None:
            return "unknown"
        try:
            n = int(len(df))
        except Exception:
            return "unknown"
        if n < _REGIME_MIN_BARS_HARD:
            return "unknown"
        if n >= _REGIME_DETECT_MIN_BARS:
            try:
                from backend.market_regime import _detect as _det
                return _det(symbol, df).regime
            except Exception:
                return "unknown"
        # ここからフォールバック: ADX/ATR は信頼できないので EMA20 + 最新 close だけ見る。
        try:
            close = df["close"]
            span = max(5, min(20, n // 2))
            ema = close.ewm(span=span, adjust=False).mean()
            e_now = float(ema.iloc[-1])
            back = max(1, min(n - 1, span // 2))
            e_prev = float(ema.iloc[-back - 1])
            c_now = float(close.iloc[-1])
            if e_prev <= 0 or e_now != e_now:
                return "ranging"
            slope = (e_now - e_prev) / e_prev
            # しきい値は控えめ。前場の平均的な swing 幅 (0.3〜0.5%) を参考に 0.2%。
            if slope > 0.002 and c_now > e_now:
                return "trending_up"
            if slope < -0.002 and c_now < e_now:
                return "trending_down"
            return "ranging"
        except Exception:
            return "unknown"

    async def _try_open_position(
        self,
        strategy: StrategyBase,
        symbol: str,
        df: pd.DataFrame,
        latest_price: float,
        now: datetime,
        *,
        is_long: bool,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[bool, str]:
        sid = strategy.meta.id
        halt_info = self._load_halt_file()
        if halt_info.get("halt", False):
            reason = str(halt_info.get("reason", "manual_halt"))
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="manual_halt",
                edge_score=self._expected_edge_score(strategy),
                detail={"halt_reason": reason},
            )
            logger.info("ENTRY BLOCKED (manual halt): %s [%s] reason=%s", symbol, sid, reason)
            return False, "manual_halt"

        # 2026-04-27: 全停止（halt: true）とは別に、(symbol, strategy_name) 単位の
        # 一時停止を halt ファイルの `paused_pairs` で受け付ける。WF 検証で 0/5 OOS
        # positive のような明確な劣化が見つかったペアを、universe_active を触らず
        # に止めるための仕組み（research_canonical_sync で書き戻されない）。
        # スキーマ: {"paused_pairs": [{"symbol": "1605.T", "strategy_name": "MacdRci",
        #   "reason": "...", "until": "2026-05-04"}, ...]}.
        # `until`（YYYY-MM-DD）が今日の日付より前なら自動失効。strategy_name は
        # 戦略クラス名（"JP" プレフィックスを除いた "MacdRci" / "Scalp" 等）で照合。
        paused_pairs = halt_info.get("paused_pairs") or []
        if paused_pairs:
            try:
                cls_name = type(strategy).__name__
                strategy_kind = cls_name[2:] if cls_name.startswith("JP") else cls_name
            except Exception:
                strategy_kind = ""
            for entry in paused_pairs:
                if not isinstance(entry, dict):
                    continue
                if entry.get("symbol") != symbol:
                    continue
                wanted_strategy = entry.get("strategy_name") or entry.get("strategy")
                if wanted_strategy and wanted_strategy != strategy_kind:
                    continue
                until_raw = entry.get("until")
                if isinstance(until_raw, str) and until_raw:
                    try:
                        until_dt = datetime.strptime(until_raw, "%Y-%m-%d")
                        if now.date() > until_dt.date():
                            continue  # 失効済み
                    except Exception:
                        pass
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="paused_pair",
                    edge_score=self._expected_edge_score(strategy),
                    detail={
                        "strategy_kind": strategy_kind,
                        "pause_reason": entry.get("reason", ""),
                        "until": entry.get("until", ""),
                    },
                )
                logger.info(
                    "ENTRY BLOCKED (paused pair): %s [%s] strategy=%s reason=%s until=%s",
                    symbol, sid, strategy_kind,
                    entry.get("reason", ""), entry.get("until", ""),
                )
                return False, "paused_pair"

        # 2026-04-28: sector_strength gate (C7) — weak セクター × 順張り戦略の skip。
        # `data/sector_strength_latest.json`（毎朝 cron で再生成）の symbol→sectors→label
        # を引き、long entry 戦略が weak セクターのとき blockする/記録する。
        # observation_pairs (force_paper=true) はバイパス。
        # 段階導入: off / dry_log（A/B 観察）/ on（実 block）。
        if (
            _SECTOR_STRENGTH_MODE != "off"
            and is_long
        ):
            try:
                cls_name = type(strategy).__name__
                strategy_kind_for_sector = cls_name[2:] if cls_name.startswith("JP") else cls_name
            except Exception:
                strategy_kind_for_sector = ""
            if strategy_kind_for_sector in _SECTOR_STRENGTH_LONG_STRATEGIES:
                sector_label, sector_names, med_5d = self._get_sector_strength(symbol)
                if sector_label == "weak":
                    detail_payload = {
                        "strategy_kind": strategy_kind_for_sector,
                        "sectors": sector_names,
                        "label": sector_label,
                        "median_5d_pct": med_5d,
                        "mode": _SECTOR_STRENGTH_MODE,
                        "side": "long",
                    }
                    if _SECTOR_STRENGTH_MODE == "on":
                        self._record_skip_event(
                            symbol=symbol,
                            strategy_id=sid,
                            reason="weak_sector_long_block",
                            edge_score=self._expected_edge_score(strategy),
                            detail=detail_payload,
                        )
                        logger.info(
                            "ENTRY BLOCKED (weak sector): %s [%s] strategy=%s sectors=%s med_5d=%s",
                            symbol, sid, strategy_kind_for_sector, sector_names, med_5d,
                        )
                        return False, "weak_sector_long_block"
                    # dry_log: skip_event に記録するが約定は止めない
                    self._record_skip_event(
                        symbol=symbol,
                        strategy_id=sid,
                        reason="weak_sector_long_block_dry",
                        edge_score=self._expected_edge_score(strategy),
                        detail=detail_payload,
                    )

        # 2026-04-27: 朝寄り直後 (09:00-09:30 既定) の時間ベースロック。バー数ベースの
        # `_UNKNOWN_REGIME_GUARD` だけだと feed が 1 分足を 20 本返した瞬間にロックが
        # 解け、まだ「寄り後の値動きが落ち着いていない」段階でエントリーが入ってしまう。
        # 4/27 朝の 6 件 -12,700 円が代表例。env `JP_MORNING_WARMUP_BLOCK_MIN` で調整。
        if _is_morning_warmup_window(now):
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="morning_warmup_block",
                edge_score=self._expected_edge_score(strategy),
                detail={
                    "now": now.strftime("%H:%M"),
                    "block_until_min": _MORNING_WARMUP_BLOCK_MIN,
                    "side": "long" if is_long else "short",
                },
            )
            logger.info(
                "ENTRY BLOCKED (morning warmup): %s [%s] now=%s window=09:00-09:%02d",
                symbol, sid, now.strftime("%H:%M"), _MORNING_WARMUP_BLOCK_MIN,
            )
            return False, "morning_warmup_block"

        # 2026-04-24: レジーム "unknown" での発注は実績的に負け超過（4/24 朝 6件 -9,270 円）。
        # place_order より前にレジームを確定し、未成立なら見送って skip_event に残す。
        # 多段判定のため unknown になるのは `len < _REGIME_MIN_BARS_HARD`（1m: 20 分、5m: 1h40m）
        # の「真のウォームアップ」時のみ。フォールバック範囲は trending/ranging を返す。
        entry_regime = self._detect_entry_regime(symbol, df)
        if _UNKNOWN_REGIME_GUARD_ENABLED and entry_regime == "unknown":
            bars_n = int(len(df)) if df is not None else 0
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="unknown_regime_guard",
                edge_score=self._expected_edge_score(strategy),
                detail={
                    "bars": bars_n,
                    "min_bars_hard": _REGIME_MIN_BARS_HARD,
                    "min_bars_full": _REGIME_DETECT_MIN_BARS,
                    "side": "long" if is_long else "short",
                },
            )
            logger.info(
                "ENTRY BLOCKED (unknown regime): %s [%s] bars=%d/%d (hard) side=%s",
                symbol, sid, bars_n, _REGIME_MIN_BARS_HARD,
                "long" if is_long else "short",
            )
            return False, "unknown_regime_guard"

        # 2026-04-27: 保有中の早期撤退と対称に、新規エントリー側でも adverse レジームを
        # 拒否する。例えば trending_down 中の新規ロング、trending_up 中の新規ショート
        # など。MacdRci の「下げ過ぎからの反発」狙いを潰す可能性があるため既定 OFF。
        # 4/27 朝のような連敗が再発したら env `JP_ADVERSE_REGIME_ENTRY_BLOCK=1` で即抑止。
        if _ADVERSE_REGIME_ENTRY_BLOCK:
            adverse = _ADVERSE_REGIME_LONG if is_long else _ADVERSE_REGIME_SHORT
            if entry_regime in adverse:
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="adverse_regime_entry",
                    edge_score=self._expected_edge_score(strategy),
                    detail={
                        "regime": entry_regime,
                        "side": "long" if is_long else "short",
                        "adverse_set": sorted(adverse),
                    },
                )
                logger.info(
                    "ENTRY BLOCKED (adverse regime): %s [%s] regime=%s side=%s",
                    symbol, sid, entry_regime,
                    "long" if is_long else "short",
                )
                return False, "adverse_regime_entry"

        risk_ok, risk_detail = self._check_daily_loss_guard(now, symbol, latest_price)
        if not risk_ok:
            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="daily_loss_guard",
                edge_score=self._expected_edge_score(strategy),
                detail=risk_detail,
            )
            logger.info(
                "ENTRY BLOCKED (daily loss guard): %s [%s] loss=%+.0f limit=-%.0f",
                symbol,
                sid,
                float(risk_detail.get("total_pnl_jpy", 0.0)),
                float(risk_detail.get("loss_limit_jpy", 0.0)),
            )
            return False, "daily_loss_guard"

        tier = get_tier(_JP_CAPITAL_JPY)
        position_value = _JP_CAPITAL_JPY * tier.margin * tier.position_pct
        alloc_w = 0.0
        try:
            alloc_w = float((strategy.meta.params or {}).get("allocation_weight", 0.0) or 0.0)
        except Exception:
            alloc_w = 0.0
        if 0.0 < alloc_w <= 1.0:
            total_budget = _JP_CAPITAL_JPY * tier.margin * min(max(tier.position_pct * tier.max_concurrent, tier.position_pct), 1.0)
            position_value = total_budget * alloc_w
        position_value *= _LIVE_POSITION_SCALE

        # 2026-05-02: D5 — MicroScalp 専用 lot 縮小。MicroScalp は 1m スキャル
        # で短期決着 (timeout 2 分) のため、1 銘柄に大きな余力を割り当てる必要が
        # ない。むしろ 4-6 銘柄並走で signal 数を稼ぐ方が PnL 期待値が高い
        # (D3 検証結果: per-symbol +1,000-7,500 円/日 × 余力圧縮で +1,500-3,000 円/日)。
        # `_JP_MICRO_SCALP_POSITION_PCT` (default 0.30 = 1 銘柄 99 万 × 0.30 = 29.7 万)
        # で固定し、高額銘柄 (株価 3,000 円超) は構造的に 100 株未達で skip される。
        if "jp_micro_scalp_" in sid.lower():
            ms_pct = float(os.getenv("JP_MICRO_SCALP_POSITION_PCT", "0.30"))
            ms_pct = max(0.10, min(0.50, ms_pct))
            position_value = _JP_CAPITAL_JPY * tier.margin * ms_pct * _LIVE_POSITION_SCALE
        liq_cap = LIQUIDITY_MAX_POSITION.get(symbol)
        if liq_cap is not None:
            position_value = min(position_value, liq_cap)
        try:
            cash = float(self._broker.get_account().cash)
            position_value = min(position_value, max(0.0, cash * 0.98))
        except Exception:
            cash = 0.0
        qty = int(position_value / latest_price / 100) * 100 if latest_price > 0 else 0

        # 2026-05-02: D2 — 同時保有合計が信用枠の 85% を超える entry を抑制。
        # 5/1 の余力枯渇 (10:22-10:30 で 9984/9468 の 8 件 skip) を構造的に解決する。
        # 既存ポジ合計を計測し、新ポジ予定 cost を加えて信用枠 cap で判定する。
        if qty >= 100:
            buying_power = _JP_CAPITAL_JPY * tier.margin
            cumulative_locked = 0.0
            high_cost_pos_n = 0
            try:
                for _s, _by_sid in self._positions.items():
                    for _sid_inner, _pos in _by_sid.items():
                        pos_val = float(_pos.qty) * float(_pos.entry_price)
                        cumulative_locked += pos_val
                        if pos_val >= _HIGH_COST_THRESHOLD_JPY:
                            high_cost_pos_n += 1
            except Exception:
                pass
            new_pos_cost = float(qty) * float(latest_price)
            cap_limit = buying_power * _MAX_CONCURRENT_VALUE_RATIO

            # 累積保有 + 新規予定が信用枠の 85% を超えるなら entry 抑制
            if cumulative_locked + new_pos_cost > cap_limit:
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="concurrent_value_cap",
                    edge_score=self._expected_edge_score(strategy),
                    detail={
                        "cumulative_locked": round(cumulative_locked),
                        "new_pos_cost": round(new_pos_cost),
                        "buying_power": round(buying_power),
                        "max_ratio": _MAX_CONCURRENT_VALUE_RATIO,
                        "limit": round(cap_limit),
                        "active_n": int(sum(len(by) for by in self._positions.values())),
                    },
                )
                logger.info(
                    "ENTRY BLOCKED (concurrent_value_cap): %s [%s] locked=%.0f + new=%.0f > limit=%.0f",
                    symbol, sid, cumulative_locked, new_pos_cost, cap_limit,
                )
                return False, "concurrent_value_cap"

            # 高額ポジション (>= 50万) の同時保有制限
            if (
                new_pos_cost >= _HIGH_COST_THRESHOLD_JPY
                and high_cost_pos_n >= _HIGH_COST_MAX_CONCURRENT
            ):
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="high_cost_concurrent_cap",
                    edge_score=self._expected_edge_score(strategy),
                    detail={
                        "new_pos_cost": round(new_pos_cost),
                        "threshold": _HIGH_COST_THRESHOLD_JPY,
                        "current_high_cost_pos_n": high_cost_pos_n,
                        "max_concurrent": _HIGH_COST_MAX_CONCURRENT,
                    },
                )
                logger.info(
                    "ENTRY BLOCKED (high_cost_concurrent_cap): %s [%s] new=%.0f current_hc=%d/%d",
                    symbol, sid, new_pos_cost, high_cost_pos_n, _HIGH_COST_MAX_CONCURRENT,
                )
                return False, "high_cost_concurrent_cap"

        if qty < 100:
            # 2026-04-22: `insufficient_lot` が 4/22 に 81 回発生。ghost 起因か cash 拘束
            # 起因かを後追いで切り分けられるよう、判定要素をまとめて構造化ログに残す。
            required_min_cash = latest_price * 100.0  # 単元株=100
            tracked_n = sum(len(by) for by in self._positions.values())
            held_value = 0.0
            try:
                for _s, by in self._positions.items():
                    for _sid, _pos in by.items():
                        held_value += float(_pos.qty) * float(_pos.entry_price)
            except Exception:
                pass
            try:
                broker_positions = list(self._broker.get_positions())
            except Exception:
                broker_positions = []
            ghost_symbols: list[str] = []
            tracked_syms = {s for s, by in self._positions.items() for _sid in by.keys()}
            for p in broker_positions:
                if p.symbol not in tracked_syms:
                    ghost_symbols.append(p.symbol)
            detail = {
                "cash": cash,
                "position_value": position_value,
                "required_min_cash": required_min_cash,
                "liq_cap": liq_cap,
                "alloc_w": alloc_w,
                "tier_label": getattr(tier, "label", "?"),
                "max_concurrent": int(getattr(tier, "max_concurrent", 0) or 0),
                "tracked_n": tracked_n,
                "held_value": held_value,
                "broker_positions_n": len(broker_positions),
                "ghost_symbols": ghost_symbols,
            }
            logger.warning(
                "insufficient_lot %s [%s] @%.1f qty_calc=%d cash=%.0f pos_val=%.0f "
                "req_min_cash=%.0f alloc_w=%.2f tier=%s tracked=%d held=%.0f "
                "broker_pos=%d ghosts=%s",
                symbol, sid, latest_price, qty, cash, position_value,
                required_min_cash, alloc_w, detail["tier_label"],
                tracked_n, held_value, len(broker_positions),
                ",".join(ghost_symbols) if ghost_symbols else "none",
            )
            # ghost が検知された場合は即時に売却を試みて救済する。tracked より broker の方が
            # 多いのは「コールバック例外 / 記録漏れ」で自明の異常。翌日までほっとくと
            # reset までずっと insufficient_lot を出し続けるため早期回復させる。
            if ghost_symbols:
                await self._sell_ghosts(ghost_symbols, reason="insufficient_lot_rescue")

            self._record_skip_event(
                symbol=symbol,
                strategy_id=sid,
                reason="insufficient_lot",
                edge_score=self._expected_edge_score(strategy),
                detail=detail,
            )
            return False, "insufficient_lot"

        if is_long:
            order = await self._broker.place_order(symbol, "buy", float(qty), latest_price)
            if order.status != "filled":
                logger.warning("Order rejected [%s/%s]: %s", symbol, sid, order.note)
                note = str(getattr(order, "note", "") or "").lower()
                if "insufficient" in note:
                    self._record_skip_event(
                        symbol=symbol,
                        strategy_id=sid,
                        reason="insufficient_cash",
                        edge_score=self._expected_edge_score(strategy),
                    )
                    return False, "insufficient_cash"
                self._record_skip_event(
                    symbol=symbol,
                    strategy_id=sid,
                    reason="order_rejected",
                    edge_score=self._expected_edge_score(strategy),
                )
                return False, "order_rejected"
        # short: PaperBrokerは空売り非対応のため内部管理のみ（ペーパー検証目的）

        # entry_regime は ``_detect_entry_regime`` で確定済み（guard 通過 = 有効な regime）。
        self._positions[symbol][sid] = LivePosition(
            symbol=symbol,
            strategy_id=sid,
            entry_price=latest_price,
            qty=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=now,
            side="long" if is_long else "short",
            entry_regime=entry_regime,
            regime_history=[(now.strftime("%H:%M"), entry_regime)],
            event_day=_get_event_tag(now),
        )
        logger.info("ENTRY %s [%s] qty=%d @%.1f SL=%.1f TP=%.1f regime=%s [%s]",
                    symbol, "long" if is_long else "short",
                    qty, latest_price, stop_loss, take_profit, entry_regime, sid)
        self._expand_peer_watch(symbol)
        return True, "ok"

    def _load_halt_file(self) -> dict:
        if not _HALT_FILE_PATH.exists():
            return {"halt": False}
        try:
            raw = json.loads(_HALT_FILE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception as e:
            logger.warning("Failed to read halt file %s: %s", _HALT_FILE_PATH, e)
        return {"halt": False}

    def _calc_unrealized_pnl(self, current_symbol: str, current_price: float) -> float:
        unrealized = 0.0
        for symbol, by_sid in self._positions.items():
            for pos in by_sid.values():
                px = current_price if symbol == current_symbol else float(pos.entry_price)
                if self._feed is not None and symbol != current_symbol:
                    try:
                        bars = self._feed.get_bars(symbol)
                        if bars is not None and len(bars) > 0:
                            px = float(bars["close"].iloc[-1])
                    except Exception:
                        pass
                if pos.side == "long":
                    unrealized += (px - pos.entry_price) * pos.qty
                else:
                    unrealized += (pos.entry_price - px) * pos.qty
        return float(unrealized)

    def _check_daily_loss_guard(self, now: datetime, symbol: str, latest_price: float) -> tuple[bool, dict]:
        if self._session_start_equity is None:
            try:
                self._session_start_equity = float(self._broker.get_account().equity)
            except Exception:
                self._session_start_equity = _JP_RISK_BASE_JPY
        realized = float(self._session.total_pnl)
        unrealized = self._calc_unrealized_pnl(symbol, latest_price)
        total_pnl = realized + unrealized
        base_jpy = float(_JP_RISK_BASE_JPY)
        loss_limit_jpy = float(base_jpy * _JP_DAILY_LOSS_LIMIT_PCT)
        ok = total_pnl > -loss_limit_jpy
        detail = {
            "ts": now.isoformat(),
            "risk_base_jpy": base_jpy,
            "daily_loss_limit_pct": float(_JP_DAILY_LOSS_LIMIT_PCT),
            "loss_limit_jpy": loss_limit_jpy,
            "session_start_equity": float(self._session_start_equity),
            "realized_pnl_jpy": realized,
            "unrealized_pnl_jpy": unrealized,
            "total_pnl_jpy": total_pnl,
        }
        return ok, detail

    async def _try_fallback_entry(
        self,
        *,
        blocked_strategy: StrategyBase,
        blocked_reason: str,
        now: datetime,
    ) -> None:
        """余力制約で入れない場合、期待値が十分な次点へ切り替える。"""
        blocked_score = self._expected_edge_score(blocked_strategy)
        min_required = max(_FALLBACK_MIN_ABS_SCORE, blocked_score * _FALLBACK_MIN_SCORE_RATIO)
        if self._feed is None:
            return
        candidates = sorted(self._strategies, key=self._expected_edge_score, reverse=True)
        for cand in candidates:
            if cand.meta.id == blocked_strategy.meta.id:
                continue
            if self._expected_edge_score(cand) < min_required:
                continue
            sym = cand.meta.symbol
            if self._positions.get(sym, {}).get(cand.meta.id):
                continue
            bars = self._feed.get_bars(sym)
            if bars is None or len(bars) < 30:
                continue
            try:
                signals = cand.generate_signals(bars)
            except Exception:
                continue
            sig = signals["signal"].iloc[-1]
            if sig not in (1, -2):
                continue
            px = float(bars["close"].iloc[-1])
            row = signals.iloc[-1]
            if sig == 1:
                sl = float(row.get("stop_loss", px * 0.995))
                tp = float(row.get("take_profit", px * 1.01))
                if pd.isna(sl): sl = px * 0.995
                if pd.isna(tp): tp = px * 1.01
            else:
                sl = float(row.get("stop_loss", px * 1.005))
                tp = float(row.get("take_profit", px * 0.99))
                if pd.isna(sl): sl = px * 1.005
                if pd.isna(tp): tp = px * 0.99
            entered, _ = await self._try_open_position(
                strategy=cand,
                symbol=sym,
                df=bars,
                latest_price=px,
                now=now,
                is_long=(sig == 1),
                stop_loss=sl,
                take_profit=tp,
            )
            if entered:
                logger.info(
                    "FALLBACK ENTRY: %s -> %s (reason=%s, min_score=%.1f, cand_score=%.1f)",
                    blocked_strategy.meta.symbol,
                    sym,
                    blocked_reason,
                    min_required,
                    self._expected_edge_score(cand),
                )
                return
        # 同一 (symbol, reason) での連呼は _FALLBACK_LOG_MIN_INTERVAL_SEC 秒に1回だけ。
        key = (blocked_strategy.meta.symbol, blocked_reason)
        last = self._fallback_wait_last_logged.get(key)
        should_log = (
            last is None
            or (now - last).total_seconds() >= max(1, _FALLBACK_LOG_MIN_INTERVAL_SEC)
        )
        if should_log:
            self._fallback_wait_last_logged[key] = now
            logger.info(
                "FALLBACK WAIT: %s blocked=%s min_score=%.1f (有効な次点なし, throttle=%ds)",
                blocked_strategy.meta.symbol,
                blocked_reason,
                min_required,
                _FALLBACK_LOG_MIN_INTERVAL_SEC,
            )
        self._record_skip_event(
            symbol=blocked_strategy.meta.symbol,
            strategy_id=blocked_strategy.meta.id,
            reason=f"fallback_wait:{blocked_reason}",
            edge_score=self._expected_edge_score(blocked_strategy),
            detail={"min_required": round(min_required, 2)},
        )

    def _expected_edge_score(self, strategy: StrategyBase) -> float:
        try:
            return float((strategy.meta.params or {}).get("expected_edge_score", 0.0) or 0.0)
        except Exception:
            return 0.0

    # ── sector_strength gate (C7) ──────────────────────────────────────────
    def _get_sector_strength(self, symbol: str) -> tuple[str, list[str], float | None]:
        """symbol → (label, sector_names, median_5d_pct) を返す.

        `data/sector_strength_latest.json` を mtime ベースで lazy load。
        モジュール定数 `_SECTOR_STRENGTH_RELOAD_SEC` を超えたら再ロード。
        symbol が複数セクターに属する場合は最も weak（median_5d 最小）を採用。
        """
        cache = getattr(self, "_sector_strength_cache", None)
        loaded_at = getattr(self, "_sector_strength_loaded_at", 0.0)
        import time as _time
        now_ts = _time.time()
        need_reload = (
            cache is None
            or (now_ts - loaded_at) > _SECTOR_STRENGTH_RELOAD_SEC
        )
        if need_reload:
            try:
                if _SECTOR_STRENGTH_PATH.exists():
                    payload = json.loads(_SECTOR_STRENGTH_PATH.read_text(encoding="utf-8"))
                    sector_to_meta = {}
                    for s in payload.get("sectors", []) or []:
                        nm = s.get("sector")
                        if not nm:
                            continue
                        sector_to_meta[nm] = {
                            "label": s.get("label", "unknown"),
                            "median_5d_pct": s.get("median_5d_pct"),
                        }
                    sym_to_sectors = payload.get("symbol_to_sectors") or {}
                    cache = {
                        "symbol_to_sectors": sym_to_sectors,
                        "sector_to_meta": sector_to_meta,
                    }
                else:
                    cache = {"symbol_to_sectors": {}, "sector_to_meta": {}}
            except Exception:
                cache = {"symbol_to_sectors": {}, "sector_to_meta": {}}
            self._sector_strength_cache = cache
            self._sector_strength_loaded_at = now_ts

        sector_names = (cache.get("symbol_to_sectors") or {}).get(symbol) or []
        meta_by_name = cache.get("sector_to_meta") or {}
        if not sector_names:
            return ("unknown", [], None)
        # 複数セクターに属する場合は median_5d 最小（最も weak）を採用
        best_label = "unknown"
        best_med: float | None = None
        for nm in sector_names:
            meta = meta_by_name.get(nm) or {}
            label = meta.get("label", "unknown")
            med = meta.get("median_5d_pct")
            if best_med is None or (med is not None and med < best_med):
                best_med = med
                best_label = label
        return (best_label, sector_names, best_med)

    def _set_post_loss_gate(
        self,
        symbol: str,
        strategy_id: str,
        pos: LivePosition,
        df: pd.DataFrame,
        now: datetime,
    ) -> None:
        current_regime = "unknown"
        try:
            if df is not None and len(df) >= 30:
                from backend.market_regime import _detect as _det
                current_regime = _det(symbol, df).regime
        except Exception:
            current_regime = "unknown"
        self._post_loss_gate[symbol] = {
            "triggered_at": now.isoformat(),
            "strategy_id": strategy_id,
            "entry_regime": pos.entry_regime,
            "last_regime": current_regime,
            "rotation_attempted": False,
            "stop_loss": float(pos.stop_loss),
            "entry_price": float(pos.entry_price),
        }
        logger.info(
            "POST-LOSS GATE set %s [%s]: entry_regime=%s now_regime=%s",
            symbol, strategy_id, pos.entry_regime, current_regime,
        )

    def _evaluate_post_loss_gate(
        self,
        symbol: str,
        strategy_id: str,
        df: pd.DataFrame,
        now: datetime,
    ) -> tuple[bool, dict]:
        gate = self._post_loss_gate.get(symbol)
        if not gate:
            return True, {"gate": "none"}

        # TTL チェック: 発動から一定時間が経過したら無条件で解除して再評価を許す。
        triggered_at = gate.get("triggered_at")
        if _RECHECK_TTL_MIN > 0 and isinstance(triggered_at, str):
            try:
                t0 = datetime.fromisoformat(triggered_at)
                if (now - t0).total_seconds() >= _RECHECK_TTL_MIN * 60:
                    self._post_loss_gate.pop(symbol, None)
                    logger.info(
                        "POST-LOSS GATE expired %s (ttl=%dm, since %s) — re-entry allowed",
                        symbol, _RECHECK_TTL_MIN, t0.strftime("%H:%M"),
                    )
                    return True, {"gate": "ttl_expired"}
            except Exception:
                pass

        tb = _time_bucket(now)
        time_ok = not (_RECHECK_LUNCH_BLOCK and tb == "lunch_gap")

        vol_pct = 0.0
        vol_ok = False
        try:
            c = df["close"].astype(float).tail(24)
            ret = c.pct_change().dropna()
            vol_pct = float(ret.std() * 100.0) if len(ret) >= 6 else 0.0
            vol_ok = _RECHECK_VOL_MIN_PCT <= vol_pct <= _RECHECK_VOL_MAX_PCT
        except Exception:
            vol_ok = False

        regime_now = "unknown"
        regime_ok = False
        regime_match_mode = "strict"
        try:
            if df is not None and len(df) >= 30:
                from backend.market_regime import _detect as _det
                regime_now = _det(symbol, df).regime
                baseline = gate.get("entry_regime", "unknown")
                if _RECHECK_REGIME_STRICT:
                    # 旧挙動: baseline と完全一致のときだけ OK
                    regime_ok = (baseline == "unknown") or (regime_now == baseline)
                    regime_match_mode = "strict"
                else:
                    # 新挙動 (adverse-only): adverse 方向以外なら OK
                    # 4/28 6723.T 41 件のような「entry: trending_up → now: low_vol/ranging で
                    # 永久に regime 不一致 → block 継続」を防ぐ。
                    adverse = _RECHECK_ADVERSE_REGIME_MAP.get(baseline)
                    regime_ok = (
                        baseline == "unknown"
                        or regime_now == "unknown"
                        or adverse is None
                        or regime_now != adverse
                    )
                    regime_match_mode = "adverse_only"
        except Exception:
            regime_ok = False

        detail = {
            "gate": "post_loss",
            "triggered_at": gate.get("triggered_at"),
            "strategy_id": gate.get("strategy_id"),
            "for_strategy": strategy_id,
            "time_bucket": tb,
            "time_ok": bool(time_ok),
            "vol_pct": round(vol_pct, 4),
            "vol_range_pct": [_RECHECK_VOL_MIN_PCT, _RECHECK_VOL_MAX_PCT],
            "vol_ok": bool(vol_ok),
            "entry_regime": gate.get("entry_regime", "unknown"),
            "regime_now": regime_now,
            "regime_ok": bool(regime_ok),
            "regime_match_mode": regime_match_mode,
            "rotation_attempted": bool(gate.get("rotation_attempted", False)),
        }
        ok = bool(time_ok and vol_ok and regime_ok)
        if ok:
            self._post_loss_gate.pop(symbol, None)
            logger.info(
                "POST-LOSS GATE cleared %s: time=%s vol=%.4f regime=%s",
                symbol, tb, vol_pct, regime_now,
            )
        return ok, detail

    def _expand_peer_watch(self, symbol: str) -> None:
        # エントリー時に同一セクター銘柄の監視を開始
        try:
            from backend.backtesting.trade_guard import get_correlated_symbols, get_sector
            sector = get_sector(symbol)
            peers = get_correlated_symbols(symbol)
            if peers and self._feed:
                existing = set(self._feed._symbols)
                new_peers = [p for p in peers if p not in existing and p.endswith('.T')]
                if new_peers:
                    self._feed.set_symbols(list(existing | set(new_peers)))
                    logger.info("PEER WATCH: %s → %s (%s)", symbol, new_peers, sector)
        except Exception as e:
            logger.debug("Peer watch setup error: %s", e)

    def _record_skip_event(
        self,
        *,
        symbol: str,
        strategy_id: str,
        reason: str,
        edge_score: float,
        detail: dict | None = None,
    ) -> None:
        self._skip_events.append({
            "ts": datetime.now(JST).isoformat(),
            "symbol": symbol,
            "strategy_id": strategy_id,
            "reason": reason,
            "edge_score": float(edge_score),
            "detail": detail or {},
        })

    def _record_no_signal_diag(
        self,
        symbol: str,
        strategy_id: str,
        signals: "pd.DataFrame",
        now: datetime,
        *,
        interval_min: int = 30,
    ) -> None:
        """信号未成立バーの MACD/RCI 中間値を定期的にスナップショットして保存する。

        - 毎バー記録するとログが爆発するため、(symbol, strategy_id) 単位で
          ``interval_min`` 分ごとに最大 1 件のみ記録する。
        - 記録対象は MacdRci 系戦略（DataFrame に ``macd`` / ``rci_*`` 列があるもの）のみ。
          それ以外の戦略では NaN が並ぶだけなので早期 return する。
        """
        try:
            key = (symbol, strategy_id)
            last = self._no_signal_diag_last.get(key)
            if last is not None and (now - last) < timedelta(minutes=int(interval_min)):
                return
            if signals is None or len(signals) == 0:
                return
            last_row = signals.iloc[-1]
            # MacdRci 戦略以外（macd 列が無い）はスキップ
            if "macd" not in signals.columns:
                return

            def _f(val) -> float | None:
                try:
                    v = float(val)
                    if v != v:  # NaN
                        return None
                    return v
                except Exception:
                    return None

            rci_cols = [c for c in signals.columns if c.startswith("rci_") and c not in {
                "rci_gc_bar", "rci_dc_bar", "rci_short_slope",
                "rci_gc_slope_arctan_deg", "rci_slope_at_gc_bar", "rci_slope_at_dc_bar",
            }]
            rci_snapshot = {c: _f(last_row.get(c)) for c in rci_cols}
            macd_val = _f(last_row.get("macd"))
            macd_sig = _f(last_row.get("macd_sig"))
            macd_hist = _f(last_row.get("macd_hist"))
            rci_slope = _f(last_row.get("rci_short_slope"))
            # 未達の切り分けヒント
            missing = []
            if macd_val is not None and macd_val <= 0:
                missing.append("macd<=0")
            if macd_sig is not None and macd_sig <= 0:
                missing.append("macd_sig<=0")
            if macd_hist is not None and macd_hist <= 0:
                missing.append("macd_hist<=0")
            up_cnt = sum(1 for v in rci_snapshot.values() if v is not None and v > 0)
            if rci_snapshot and up_cnt < max(1, len(rci_snapshot) // 2 + 1):
                missing.append(f"rci_majority({up_cnt}/{len(rci_snapshot)})")
            # セッション外ヒント
            minute = now.hour * 60 + now.minute
            in_entry_session = (
                (9 * 60 + 5 <= minute <= 11 * 60 + 25)
                or (12 * 60 + 30 <= minute <= 14 * 60 + 30)
            )
            if not in_entry_session:
                missing.append("out_of_session")

            detail = {
                "macd": round(macd_val, 4) if macd_val is not None else None,
                "macd_sig": round(macd_sig, 4) if macd_sig is not None else None,
                "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
                "rci": {k: (round(v, 2) if v is not None else None) for k, v in rci_snapshot.items()},
                "rci_short_slope": round(rci_slope, 3) if rci_slope is not None else None,
                "missing": missing,
                "in_entry_session": bool(in_entry_session),
                "last_bar_ts": str(signals.index[-1]),
            }
            self._skip_events.append({
                "ts": now.isoformat(),
                "symbol": symbol,
                "strategy_id": strategy_id,
                "reason": "no_signal_diag",
                "edge_score": 0.0,
                "detail": detail,
            })
            self._no_signal_diag_last[key] = now
        except Exception as e:
            logger.debug("no_signal_diag failed [%s/%s]: %s", symbol, strategy_id, e)

    async def _close_position(
        self,
        pos: LivePosition,
        price: float,
        reason: str,
        now: datetime,
    ) -> None:
        if pos.side == "long":
            order = await self._broker.place_order(pos.symbol, "sell", float(pos.qty), price)
            if order.status != "filled":
                logger.warning("Sell failed [%s]: %s", pos.symbol, order.note)
                return
            pnl = (price - pos.entry_price) * pos.qty
        else:  # short: 売値 - 買戻し値（内部管理のみ）
            pnl = (pos.entry_price - price) * pos.qty
        # 決済時のレジームを記録（振り返り用）
        exit_regime = "unknown"
        try:
            if self._feed:
                bars = self._feed.get_bars(pos.symbol)
                if bars is not None and len(bars) >= 30:
                    from backend.market_regime import _detect as _det
                    exit_regime = _det(pos.symbol, bars).regime
        except Exception:
            pass

        regime_changed = len(pos.regime_history) > 1
        trade = LiveTrade(
            symbol=pos.symbol,
            strategy_id=pos.strategy_id,
            entry_price=pos.entry_price,
            exit_price=price,
            qty=pos.qty,
            pnl=pnl,
            entry_time=pos.entry_time,
            exit_time=now,
            exit_reason=reason,
            side=pos.side,
            entry_regime=pos.entry_regime,
            exit_regime=exit_regime,
            regime_changed=regime_changed,
            regime_history=list(pos.regime_history),
            event_day=pos.event_day,
        )
        # サブセッションに記録（なければセッション直接）
        if self._current_subsession is not None:
            self._current_subsession.trades.append(trade)
        else:
            # クールダウン中にポジションがクローズされた場合は前サブセッションの末尾に追記
            if self._session.subsessions:
                self._session.subsessions[-1].trades.append(trade)
        pnl_sign = "+" if pnl >= 0 else ""
        logger.info("EXIT %s [%s] @%.1f pnl=%s%.0f円 reason=%s [%s]",
                    pos.symbol, pos.side, price, pnl_sign, pnl, reason, pos.strategy_id)

        # 曜日×時間帯の記録
        try:
            from backend.backtesting.trade_guard import record_trade_timing
            record_trade_timing(now, pnl)
        except Exception:
            pass

        # 連敗検出 → レジームチェック＆戦略切替トリガー
        if pnl < 0:
            self._consecutive_losses = getattr(self, "_consecutive_losses", 0) + 1
            if self._consecutive_losses >= 3:
                logger.info("連敗%d回 → レジームチェック発動", self._consecutive_losses)
                self._consecutive_losses = 0
                asyncio.ensure_future(self._trigger_regime_recheck())
        else:
            self._consecutive_losses = 0

    async def _trigger_regime_recheck(self) -> None:
        """連敗時にレジームチェック＆戦略切替を実行する。"""
        try:
            from backend.lab.daily_prep import midday_regime_check
            if self._feed:
                result = await midday_regime_check(self, self._feed)
                if result:
                    logger.info("連敗トリガー戦略切替: %s", result)
                    if self._notify:
                        await self._notify(
                            "連敗トリガー",
                            f"3連敗検出 → レジームチェック実行\n{result}",
                            category="alert",
                            source="jp_live_runner._trigger_regime_recheck",
                        )
        except Exception as e:
            logger.warning("連敗レジームチェック失敗: %s", e)

    # ── Session summary ───────────────────────────────────────────────────────

    def _build_strategy_daily_rows(self) -> list[dict]:
        """当日トレードから手法別日次集計を生成する。"""
        rows: list[dict] = []
        grouped: dict[tuple[str, str], list[LiveTrade]] = defaultdict(list)
        for t in self._session.all_trades:
            grouped[(t.strategy_id, t.symbol)].append(t)

        for (strategy_id, symbol), trades in grouped.items():
            pnls = [t.pnl for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            hold_mins = [
                max((t.exit_time - t.entry_time).total_seconds() / 60.0, 0.0)
                for t in trades
            ]
            reasons: dict[str, int] = {}
            entry_regimes: dict[str, int] = {}
            exit_regimes: dict[str, int] = {}
            time_buckets: dict[str, int] = {}
            for t in trades:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
                entry_regimes[t.entry_regime] = entry_regimes.get(t.entry_regime, 0) + 1
                exit_regimes[t.exit_regime] = exit_regimes.get(t.exit_regime, 0) + 1
                bucket = _time_bucket(t.entry_time)
                time_buckets[bucket] = time_buckets.get(bucket, 0) + 1

            rows.append({
                "experiment_tag": self._experiment_tag,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "num_trades": len(trades),
                "wins": wins,
                "win_rate": (wins / len(trades) * 100.0) if trades else 0.0,
                "pnl_jpy": float(sum(pnls)),
                "gross_profit_jpy": float(sum(p for p in pnls if p > 0)),
                "gross_loss_jpy": float(sum(p for p in pnls if p <= 0)),
                "avg_hold_min": (sum(hold_mins) / len(hold_mins)) if hold_mins else 0.0,
                "min_hold_min": min(hold_mins) if hold_mins else 0.0,
                "max_hold_min": max(hold_mins) if hold_mins else 0.0,
                "entry_regimes": entry_regimes,
                "exit_regimes": exit_regimes,
                "time_buckets": time_buckets,
                "reasons": reasons,
            })
        rows.sort(key=lambda r: r["pnl_jpy"], reverse=True)
        return rows

    def _build_trade_execution_rows(self) -> list[dict]:
        """当日約定の明細行を生成する。"""
        rows: list[dict] = []
        for t in self._session.all_trades:
            hold_min = max((t.exit_time - t.entry_time).total_seconds() / 60.0, 0.0)
            rows.append({
                "experiment_tag": self._experiment_tag,
                "strategy_id": t.strategy_id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat(),
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "qty": int(t.qty),
                "pnl_jpy": float(t.pnl),
                "hold_min": float(hold_min),
                "exit_reason": t.exit_reason,
                "entry_regime": t.entry_regime,
                "exit_regime": t.exit_regime,
                "regime_changed": bool(t.regime_changed),
                "event_day": t.event_day,
            })
        rows.sort(key=lambda r: r["entry_time"])
        return rows

    def _build_subsession_rows(self) -> list[dict]:
        """当日サブセッションを保存用dictに変換する。"""
        rows: list[dict] = []
        for ss in self._session.subsessions:
            strategies = sorted({t.strategy_id for t in ss.trades})
            rows.append({
                "start_time": ss.start_time.isoformat(),
                "end_time": ss.end_time.isoformat() if ss.end_time else None,
                "reason": ss.reason,
                "num_trades": len(ss.trades),
                "win_rate": ss.win_rate * 100.0,
                "pnl_jpy": float(ss.pnl),
                "strategies": strategies,
            })
        rows.sort(key=lambda r: r["start_time"])
        return rows

    async def _send_session_summary(self) -> None:
        s = self._session
        all_trades = s.all_trades
        strategy_rows = self._build_strategy_daily_rows()
        execution_rows = self._build_trade_execution_rows()
        subsession_rows = self._build_subsession_rows()
        skip_reason_counts: dict[str, int] = {}
        for ev in self._skip_events:
            rs = str(ev.get("reason", ""))
            skip_reason_counts[rs] = skip_reason_counts.get(rs, 0) + 1
        avg_hold_all = (
            sum(
                max((t.exit_time - t.entry_time).total_seconds() / 60.0, 0.0)
                for t in all_trades
            ) / len(all_trades)
            if all_trades else 0.0
        )
        lines = [
            f"📊 日本株ペーパー取引の結果（{s.date}）",
            f"運用モード: {self._experiment_tag or '通常'}",
            f"本日の損益合計: {s.total_pnl:+,.0f}円"
            f"（利益{s.gross_profit:+,.0f}円 / 損失{s.gross_loss:+,.0f}円）",
            (
                f"1回あたり平均: 利益 {s.avg_win:+,.0f}円 / 損失 {s.avg_loss:+,.0f}円"
                f"（利益対損失の比率 {abs(s.avg_win / s.avg_loss):.2f}）"
                if s.avg_loss != 0 else
                f"1回あたり平均: 利益 {s.avg_win:+,.0f}円 / 損失 {s.avg_loss:+,.0f}円"
            ),
            f"取引回数: {len(all_trades)}件 / 勝率: {s.win_rate*100:.0f}%",
            f"平均保有時間: {avg_hold_all:.1f}分",
            f"時間帯ごとの区切り: {len(s.subsessions)}回",
        ]
        if not all_trades:
            lines.append("本日は約定なし（シグナル未成立または余力/単元制約で見送り）")
        if skip_reason_counts:
            top_skips = sorted(skip_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("見送り理由: " + ", ".join([f"{k} x{v}" for k, v in top_skips]))
        for ss in s.subsessions:
            lines.append(
                f"  [{ss.start_time.strftime('%H:%M')}-{ss.end_time.strftime('%H:%M') if ss.end_time else '?'}]"
                f" {ss.reason} / 損益 {'+' if ss.pnl>=0 else ''}{ss.pnl:,.0f}円 / 取引 {len(ss.trades)}件"
            )
        if all_trades:
            best  = max(all_trades, key=lambda t: t.pnl)
            worst = min(all_trades, key=lambda t: t.pnl)
            lines.append(f"1件あたり最大利益: {best.symbol} {'+' if best.pnl>=0 else ''}{best.pnl:,.0f}円")
            lines.append(f"1件あたり最大損失: {worst.symbol} {'+' if worst.pnl>=0 else ''}{worst.pnl:,.0f}円")
        if self._session_sum_oos_snapshot is not None:
            snap = float(self._session_sum_oos_snapshot)
            beat = s.total_pnl >= snap
            lines.append(
                f"事前検証の目安（日次）: {snap:+,.0f}円 → 当日の結果は {'目安達成' if beat else '目安未達'}"
            )

        message = "\n".join(lines)
        logger.info("Session summary:\n%s", message)

        # 通知前に永続化（再起動や夜間集計失敗でも取りこぼさない）
        try:
            from backend.lab.paper_backtest_sync import finalize_paper_session
            finalize_paper_session(s.date, float(s.total_pnl), self._session_sum_oos_snapshot)
        except Exception as e:
            logger.warning("paper_validation handoff update: %s", e)

        try:
            from backend.storage.db import (
                replace_jp_subsessions,
                save_daily_summary,
                save_jp_signal_skip_events,
                save_jp_trade_daily,
                save_jp_trade_executions,
            )
            replace_jp_subsessions(s.date, subsession_rows)
            save_jp_trade_executions(s.date, execution_rows)
            save_jp_trade_daily(s.date, strategy_rows)
            try:
                save_jp_signal_skip_events(s.date, list(self._skip_events))
            except Exception as e:
                logger.warning("save_jp_signal_skip_events failed: %s", e)
            save_daily_summary(
                s.date,
                message,
                {
                    "jp_session_pnl": s.total_pnl,
                    "total_strategies": len(strategy_rows),
                    "positive_strategies": sum(1 for r in strategy_rows if r["pnl_jpy"] > 0),
                    "best_strategy": (
                        f'{strategy_rows[0]["strategy_id"]} {strategy_rows[0]["symbol"]}'
                        if strategy_rows else ""
                    ),
                    "best_pnl_jpy": strategy_rows[0]["pnl_jpy"] if strategy_rows else 0.0,
                    "skip_reason_counts": skip_reason_counts,
                    "skip_events_count": len(self._skip_events),
                },
            )
        except Exception as e:
            logger.error("JP session persistence error: %s", e)

        if self._notify:
            try:
                await self._notify(
                    "JP株紙トレ結果",
                    message,
                    category="jp_paper",
                    source="jp_live_runner._send_session_summary",
                    context={"date": s.date, "num_trades": len(all_trades)},
                )
                await self._maybe_notify_ab_progress()
            except Exception as e:
                logger.error("Pushover error: %s", e)

    def _resolve_experiment_tag(self, date_str: str) -> str:
        """環境変数でA/B実験モードを決定する。"""
        mode = os.environ.get("JP_PAPER_EXPERIMENT_MODE", "").strip().upper()
        if mode in {"A", "B"}:
            return mode
        if mode == "AB":
            # 日替わりで A/B を回す（奇数日=A, 偶数日=B）
            try:
                day = int(date_str.split("-")[-1])
            except Exception:
                day = datetime.now(JST).day
            return "A" if day % 2 == 1 else "B"
        return ""

    async def _maybe_notify_ab_progress(self) -> None:
        """A/B実験の進捗と完了をPushover通知する。"""
        if not self._notify:
            return
        mode = os.environ.get("JP_PAPER_EXPERIMENT_MODE", "").strip().upper()
        if mode != "AB":
            return
        try:
            from backend.storage.db import get_jp_trade_experiment_progress
            prog = get_jp_trade_experiment_progress(days=90)
            target = int(os.environ.get("JP_PAPER_EXPERIMENT_TARGET_DAYS", self.AB_TARGET_CYCLES))
            a = prog.get("A", 0)
            b = prog.get("B", 0)
            await self._notify(
                "日本株A/B検証の進捗",
                f"Aパターン: {a}/{target}日、Bパターン: {b}/{target}日（本日: {self._experiment_tag or '未設定'}）",
                category="jp_paper",
                source="jp_live_runner._maybe_notify_ab_progress",
            )
            if a >= target and b >= target:
                done_flag = Path(__file__).resolve().parent.parent.parent / "data" / "jp_ab_done.flag"
                if not done_flag.exists():
                    await self._notify(
                        "日本株A/B検証が完了",
                        f"A・Bともに {target} 日の検証が完了しました。結果の振り返りをお願いします。",
                        category="jp_paper",
                        source="jp_live_runner._maybe_notify_ab_progress",
                    )
                    done_flag.write_text(datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.error("AB progress notify error: %s", e)
