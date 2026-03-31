"""JP株リアルタイム・ペーパートレード実行エンジン.

JPRealtimeFeed からバーを受け取り、登録済み戦略のシグナルを計算して
PaperBroker 経由でペーパー注文を出す。

セッション終了（15:30 JST）後にその日の損益をまとめて Pushover 通知する。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable

import pandas as pd

from backend.brokers.paper_broker import PaperBroker
from backend.capital_tier import get_tier, LIQUIDITY_MAX_POSITION
from backend.feeds.jp_realtime_feed import JPRealtimeFeed, JST, is_market_open
from backend.strategies.base import StrategyBase

# T1資金設定（松井一日信用 最低保証金）
_JP_CAPITAL_JPY: float = 300_000.0

logger = logging.getLogger(__name__)

_CLOSE_AFTERNOON = datetime.strptime("15:30", "%H:%M").time()


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


class JPLiveRunner:
    """場中リアルタイム・ペーパートレードエンジン."""

    # サブセッション損益ルール
    # 損失上限のみ設定 — 利益上限は撤廃（良い日は前場いっぱい稼ぎ切る）
    SUBSESSION_LOSS_LIMIT_JPY:   float = -3_000.0   # -3,000円でサブセッション終了（30万資金の-1%）
    SUBSESSION_PROFIT_TARGET_JPY: float = float("inf")  # 利益上限なし
    SUBSESSION_COOLDOWN_MIN:      int   = 30         # 損失上限発動後の再開待ち（分）

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

    # ── Public API ────────────────────────────────────────────────────────────

    def set_feed(self, feed: JPRealtimeFeed) -> None:
        self._feed = feed
        feed.on_bar(self._on_bar)

    def set_strategies(self, strategies: list[StrategyBase]) -> None:
        self._strategies = strategies
        logger.info("JP live runner: %d strategies loaded", len(strategies))

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
        """yfinanceから当日の1分足データを取得する。"""
        import asyncio
        loop = asyncio.get_event_loop()

        def _fetch():
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="1d", interval="1m", auto_adjust=True)
            if df is None or df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is None:
                df.index = pd.to_datetime(df.index, utc=True)
            df.index = df.index.tz_convert("Asia/Tokyo")
            return df[["open", "high", "low", "close", "volume"]]

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _fetch), timeout=15
            )
        except Exception:
            return None

    def get_session(self) -> dict:
        s = self._session
        in_cooldown = (self._resume_after is not None
                       and datetime.now(JST) < self._resume_after)
        resume_at = (self._resume_after.strftime("%H:%M")
                     if self._resume_after else None)
        return {
            "date":           s.date,
            "total_pnl":      s.total_pnl,
            "gross_profit":   s.gross_profit,
            "gross_loss":     s.gross_loss,
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
                last_date = today
                logger.info("New session: %s", today)

            # クールダウン解除チェック → 新サブセッション開始
            if self._resume_after and now >= self._resume_after:
                logger.info("Subsession cooldown ended. Resuming at %s", now.strftime("%H:%M"))
                self._resume_after       = None
                self._current_subsession = SubSession(start_time=now)

            # 15:30以降で未送信ならサマリー送信
            if (not self._session_closed
                    and now.time() >= _CLOSE_AFTERNOON
                    and self._session.all_trades):
                # 最後のサブセッションをクローズ
                if self._current_subsession and self._current_subsession.trades:
                    self._current_subsession.end_time = now
                    self._current_subsession.reason   = "session_close"
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

        # ── ポジションあり: SL/TP/セッションクローズチェック ──────────────────
        if pos:
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

            if exit_reason is None and now.time() >= _CLOSE_AFTERNOON:
                exit_reason = "session_close"

            if exit_reason:
                await self._close_position(pos, latest_price, exit_reason, now)
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
                    asyncio.ensure_future(self._notify("JP株サブセッション終了", msg))
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

        # Tier対応ポジションサイジング（松井一日信用 T1: 30万×3.3×50%）
        tier = get_tier(_JP_CAPITAL_JPY)
        position_value = _JP_CAPITAL_JPY * tier.margin * tier.position_pct
        liq_cap = LIQUIDITY_MAX_POSITION.get(symbol)
        if liq_cap is not None:
            position_value = min(position_value, liq_cap)
        qty = int(position_value / latest_price / 100) * 100
        if qty < 100:
            logger.debug("Insufficient capital for %s @%.1f (need %d lot)", symbol, latest_price, 100)
            return

        if is_long:
            order = await self._broker.place_order(symbol, "buy", float(qty), latest_price)
            if order.status != "filled":
                logger.warning("Order rejected [%s/%s]: %s", symbol, sid, order.note)
                return
        # short: PaperBrokerは空売り非対応のため内部管理のみ（ペーパー検証目的）

        self._positions[symbol][sid] = LivePosition(
            symbol=symbol,
            strategy_id=sid,
            entry_price=latest_price,
            qty=qty,
            stop_loss=sl,
            take_profit=tp,
            entry_time=now,
            side="long" if is_long else "short",
        )
        logger.info("ENTRY %s [%s] qty=%d @%.1f SL=%.1f TP=%.1f [%s]",
                    symbol, "long" if is_long else "short",
                    qty, latest_price, sl, tp, sid)

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

    # ── Session summary ───────────────────────────────────────────────────────

    async def _send_session_summary(self) -> None:
        s = self._session
        all_trades = s.all_trades
        lines = [
            f"📊 JP株ペーパー取引 {s.date}",
            f"損益合計: {s.total_pnl:+,.0f}円"
            f"（利益{s.gross_profit:+,.0f}円 / 損失{s.gross_loss:+,.0f}円）",
            f"取引数: {len(all_trades)}件 / 勝率: {s.win_rate*100:.0f}%",
            f"サブセッション数: {len(s.subsessions)}",
        ]
        for ss in s.subsessions:
            lines.append(
                f"  [{ss.start_time.strftime('%H:%M')}-{ss.end_time.strftime('%H:%M') if ss.end_time else '?'}]"
                f" {ss.reason} {'+' if ss.pnl>=0 else ''}{ss.pnl:,.0f}円 ({len(ss.trades)}件)"
            )
        if all_trades:
            best  = max(all_trades, key=lambda t: t.pnl)
            worst = min(all_trades, key=lambda t: t.pnl)
            lines.append(f"最良: {best.symbol} {'+' if best.pnl>=0 else ''}{best.pnl:,.0f}円")
            lines.append(f"最悪: {worst.symbol} {'+' if worst.pnl>=0 else ''}{worst.pnl:,.0f}円")

        message = "\n".join(lines)
        logger.info("Session summary:\n%s", message)

        if self._notify:
            try:
                await self._notify("JP株セッション終了", message)
            except Exception as e:
                logger.error("Pushover error: %s", e)
