"""資金ティア管理 — 元本に応じた戦略調整ルール.

資金が増えるにつれて:
  1. 同時保有ポジション数を増やせる（余力が増える）
  2. 板の薄い銘柄は上限を設けてスケールアウト
  3. ポジションサイズ比率を下げてリスクを抑える
  4. より流動性の高い銘柄にシフト

逆指値狩り対策:
  - SL価格を端数ずらし（キリ番に置かない）
  - バックテストにも同じオフセットを適用
"""
from __future__ import annotations
from dataclasses import dataclass, field


# ── 銘柄別流動性上限 ─────────────────────────────────────────────────────────
# 1日の出来高の1%以内に抑えると板への影響が出ない（実測値ベース）
# None = 制限なし（超大型）
LIQUIDITY_MAX_POSITION: dict[str, float | None] = {
    "9432.T":  None,          # NTT          出来高364億円/日 → 制限なし
    "8306.T":  None,          # MUFG         出来高1,167億円/日 → 制限なし
    "7203.T":  7_000_000,     # Toyota       出来高729億円/日  → 上限700万円
    "7267.T":  3_000_000,     # Honda        出来高329億円/日  → 上限300万円
    "4568.T":  2_000_000,     # DaiichiSankyo 出来高231億円/日 → 上限200万円
    "9433.T":  2_000_000,     # KDDI         出来高240億円/日  → 上限200万円
    "2413.T":    700_000,     # M3           出来高73億円/日   → 上限70万円
    "6645.T":    600_000,     # Omron        出来高66億円/日   → 上限60万円
    "3697.T":    500_000,     # SHIFT        出来高54億円/日   → 上限50万円
    "4369.T":    200_000,     # TriChem      出来高23億円/日   → 上限20万円
}


@dataclass
class CapitalTier:
    """資金ティア定義."""
    name:              str
    capital_min:       float    # このティアの下限資金（円）
    capital_max:       float    # このティアの上限資金（円）（次ティアへの昇格条件）
    margin:            float    # 信用倍率（松井一日信用: 3.3倍）
    position_pct:      float    # 1ポジションに充てる buying power の比率
    max_concurrent:    int      # 同時保有上限（銘柄数）
    allowed_symbols:   list[str] | None  # None = スクリーナーに任せる
    excluded_symbols:  list[str] = field(default_factory=list)  # 流動性不足で除外
    daily_target_jpy:  float    = 1_000  # 日次目標P&L（円）
    note:              str      = ""
    # Phase D1: Tier ごとのスリッページ増分（bps）。資金が大きくなるほど板影響でスリッページが増えるため。
    slippage_bps:      float    = 0.0
    # Phase D1: Tier ごとの推奨戦略スタイル（バックテスト promotion ガイダンスに使う）
    preferred_styles:  list[str] = field(default_factory=list)

    @property
    def buying_power(self) -> float:
        return self.capital_min * self.margin

    @property
    def max_position_jpy(self) -> float:
        return self.buying_power * self.position_pct

    def effective_position(self, symbol: str) -> float:
        """銘柄の流動性上限を考慮した実効ポジションサイズ（円）."""
        base = self.max_position_jpy
        limit = LIQUIDITY_MAX_POSITION.get(symbol)
        if limit is not None:
            return min(base, limit)
        return base

    def max_lot(self, symbol: str, price: float, lot_size: int = 100) -> int:
        """最大何株（何単元）まで買えるか."""
        pos = self.effective_position(symbol)
        qty_raw = pos / price
        return int(qty_raw // lot_size) * lot_size

    def can_pyramid(self, symbol: str, price: float, lot_size: int = 100) -> bool:
        """この銘柄でピラミッド（追加100株）が余力的に可能か."""
        return self.max_lot(symbol, price, lot_size) >= lot_size * 2

    def pyramid_max(self, symbol: str, price: float, lot_size: int = 100) -> int:
        """最大ピラミッド回数（初回エントリー除く）."""
        max_lots = self.max_lot(symbol, price, lot_size)
        if max_lots < lot_size * 2:
            return 0
        return (max_lots // lot_size) - 1  # 初回分を引く


# ── 資金ティア定義 ─────────────────────────────────────────────────────────────
TIERS: list[CapitalTier] = [
    CapitalTier(
        name            = "T1: スタート",
        capital_min     = 300_000,
        capital_max     = 1_000_000,
        margin          = 3.3,
        # 2026-04-30: 0.50 → 0.70 に拡大。`oos_daily 平均 1,000 円/銘柄` で 4 銘柄
        # paper エントリーしても +4,000 円/日 (ROI 1.3%) しか取れず、信用 99 万を
        # 活用しきれていなかった (1ポジ ~30 万、qty=100株固定) ため。`position_pct=0.70`
        # で 1ポジ ~69万 (株価 2,500-3,800円なら 200-300株) に拡大。daily_loss_guard
        # は元本 30万×3% = -9,000円 固定のままなので最大損失上限は変わらない。
        position_pct    = 0.70,
        # 2026-04-30: 3 → 5 に拡大。本日 paper signal_skip_events=0 で max_concurrent
        # 起因の reject は無いが、`universe_active` を 17→29 ペアに拡張する以上、
        # 同日 5 銘柄まで同時保有できる枠が必要 (cash 99万 / 1ポジ ~70万 = 1.4 ポジ
        # 同時で cash 上限なので、4-5 ポジ目以降は cash 制約で圧縮されるが、機会
        # ロスは最小化)。
        max_concurrent  = 5,
        allowed_symbols = None,   # スクリーナーに任せる
        excluded_symbols= ["4369.T"],  # TriChem: 流動性上限20万円がベースポジより低い → 除外
        daily_target_jpy= 5_000,  # 2026-04-30: 1,000 → 5,000 (信用枠フル活用想定の +1.7%/日)
        note            = "70%ポジ・5並列・信用 99 万円を主軸利用 (旧 50%ポジ・3並列)。daily_loss_guard は -9,000円固定。",
    ),
    CapitalTier(
        name            = "T2: 安定稼働",
        capital_min     = 1_000_000,
        capital_max     = 3_000_000,
        margin          = 3.3,
        position_pct    = 0.50,   # 1ポジ = 買付余力×50%（EXP-001 B・ラボ既定に合わせる）
        max_concurrent  = 3,      # 同時3銘柄（allocation 時は余力クランプあり）
        allowed_symbols = None,
        excluded_symbols= ["4369.T","3697.T"],  # SHIFT・TriChem: 上限割れ
        daily_target_jpy= 3_000,
        note            = "M3・Omron・Honda・MUFG・NTT・DaiichiSankyo中心。",
    ),
    CapitalTier(
        name            = "T3: 本格運用",
        capital_min     = 3_000_000,
        capital_max     = 10_000_000,
        margin          = 3.3,
        position_pct    = 0.20,   # 1ポジ = 990万×20% = 198万
        max_concurrent  = 5,      # 同時5銘柄（990万÷198万=5）
        allowed_symbols = None,
        excluded_symbols= ["4369.T","3697.T","6645.T","2413.T"],  # 薄い銘柄を除外
        daily_target_jpy= 10_000,
        note            = "大型株中心（MUFG・NTT・Toyota・Honda・DaiichiSankyo・KDDI）。",
    ),
    CapitalTier(
        name            = "T4: プロ水準",
        capital_min     = 10_000_000,
        capital_max     = 30_000_000,
        margin          = 3.3,
        position_pct    = 0.10,   # 1ポジ = 3,300万×10% = 330万
        max_concurrent  = 10,
        allowed_symbols = ["8306.T","9432.T","7203.T","7267.T","4568.T","9433.T"],
        excluded_symbols= [],
        daily_target_jpy= 50_000,
        slippage_bps    = 3.0,
        preferred_styles= ["scalp", "breakout", "swing"],
        note            = "超大型株のみ（MUFG・NTT・Toyota・Honda・DaiichiSankyo・KDDI）。J-Quants Premium推奨。",
    ),
    # Phase D1: T5/T6 — 資金が大きくなるほど日中スキャルピングは板影響で効きにくくなり、
    # swing（日足）/ breakout（出来高先行）へ比率をシフトする。position_pct は自然に逓減。
    CapitalTier(
        name            = "T5: 大型スイング",
        capital_min     = 30_000_000,
        capital_max     = 100_000_000,
        margin          = 3.3,
        position_pct    = 0.05,   # 1ポジ = 9,900万×5% ≒ 495万
        max_concurrent  = 10,
        allowed_symbols = ["8306.T","9432.T","7203.T","7267.T","4568.T","9433.T"],
        excluded_symbols= [],
        daily_target_jpy= 150_000,
        slippage_bps    = 6.0,
        preferred_styles= ["swing", "breakout"],
        note            = "3,000万円超の資金帯。スキャル比率を下げ、日足スイング中心に切替える想定。",
    ),
    CapitalTier(
        name            = "T6: 最終目標",
        capital_min     = 100_000_000,
        capital_max     = float("inf"),
        margin          = 3.3,
        position_pct    = 0.03,   # 1ポジ = 3.3億×3% ≒ 990万
        max_concurrent  = 12,
        allowed_symbols = ["8306.T","9432.T","7203.T","7267.T","4568.T","9433.T"],
        excluded_symbols= [],
        daily_target_jpy= 500_000,
        slippage_bps    = 10.0,
        preferred_styles= ["swing"],
        note            = "1億円到達以降。スイング日足中心、寄付/引けの出来高で建玉を分散させる想定。",
    ),
]


def get_tier(capital_jpy: float) -> CapitalTier:
    """現在の資金に対応するティアを返す."""
    for tier in reversed(TIERS):
        if capital_jpy >= tier.capital_min:
            return tier
    return TIERS[0]


def sl_anti_hunt_offset(price: float, sl_pct: float, side: str = "long") -> float:
    """逆指値狩り対策: SL価格をキリ番からずらす.

    キリ番（100円単位、500円単位）に指値が集中しやすいため、
    計算値から少し内側にずらして「狩られにくい」価格にする。

    Args:
        price: エントリー価格
        sl_pct: SL率（例: 0.003 = 0.3%）
        side: "long" or "short"

    Returns:
        調整済みSL価格
    """
    if side == "long":
        raw_sl = price * (1 - sl_pct)
        # キリ番（100円単位）より少し内側（+0.05%）に置く
        # 例: SL計算値が1,980円なら → 1,981円（1,980円ちょうどに置かない）
        unit = _round_unit(price)
        rounded = round(raw_sl / unit) * unit
        if rounded >= raw_sl:
            # キリ番がSL以上になってしまう場合は1単位下げて内側へ
            return rounded - unit * 0.1
        return raw_sl + unit * 0.1   # キリ番より内側（上）
    else:  # short
        raw_sl = price * (1 + sl_pct)
        unit = _round_unit(price)
        rounded = round(raw_sl / unit) * unit
        if rounded <= raw_sl:
            return rounded + unit * 0.1
        return raw_sl - unit * 0.1   # キリ番より内側（下）


def _round_unit(price: float) -> float:
    """株価に応じた値刻み単位を返す（東証の呼び値）."""
    if price < 1_000:   return 1
    if price < 3_000:   return 1
    if price < 5_000:   return 5
    if price < 10_000:  return 5
    if price < 30_000:  return 10
    if price < 50_000:  return 50
    return 100


def print_tier_summary() -> None:
    """全ティアのサマリーを表示."""
    print("=" * 70)
    print("資金ティア別 戦略ガイドライン（松井証券 一日信用・3.3倍）")
    print("=" * 70)
    for t in TIERS:
        bp = t.capital_min * t.margin
        pos = bp * t.position_pct
        print(f"\n【{t.name}】 {t.capital_min/10000:.0f}万〜{t.capital_max/10000:.0f}万円")
        print(f"  買付余力       : {bp/10000:.0f}万円")
        print(f"  1ポジション上限: {pos/10000:.0f}万円")
        print(f"  同時保有上限   : {t.max_concurrent}銘柄")
        print(f"  日次目標       : {t.daily_target_jpy:,.0f}円/日")
        if t.slippage_bps:
            print(f"  スリッページ   : +{t.slippage_bps:.1f} bps")
        if t.preferred_styles:
            print(f"  推奨スタイル   : {', '.join(t.preferred_styles)}")
        if t.excluded_symbols:
            print(f"  除外銘柄       : {', '.join(t.excluded_symbols)}（流動性不足）")
        print(f"  メモ           : {t.note}")

    print("\n  ── 銘柄別 流動性上限（1ポジション最大） ──")
    for sym, limit in LIQUIDITY_MAX_POSITION.items():
        lim_str = f"{limit/10000:.0f}万円" if limit else "制限なし"
        print(f"    {sym}: {lim_str}")
    print("=" * 70)
