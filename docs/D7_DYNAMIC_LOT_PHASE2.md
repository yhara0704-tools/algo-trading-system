# D7 Phase 2: 動的期待値駆動ロット配分 (設計メモ)

## 背景

Phase 1 (静的 lot_multiplier) は **過去 60 日実測 PnL** をベースに事前算出した
ロット倍率を universe に書き込んで配分する方式。これで `+22,273 → +41,743 円/日` の
試算 uplift が見込まれる。

しかし、**「その日一番動きが良い銘柄に集中する」** という思想を完徹するには、
当日場中の動き (出来高、ボラ、ニュース、寄り直後の動意) に応じて
**lot_multiplier を動的に更新** する Phase 2 が必要。

## 動的期待値の信号源候補

### A. 寄り直後 30 分の動意 (9:00-9:30)

- **当日 ATR 増分**: 朝の 30 分で本日 ATR が前 5 日中央値の 1.5 倍超 → mult ×1.3
- **出来高サージ**: 寄り後 30 分の出来高が前 5 日中央値の 2 倍超 → mult ×1.2
- **方向性 (上昇/下落モメンタム)**: 9:30 終値が 9:00 始値から ±2% 超 →
  順張り戦略の mult ×1.5、逆張り戦略は ×0.7

### B. ニュース / IR / TDnet イベント

- **TDnet 速報** で当該銘柄に好材料 → mult ×1.5 (既存 `data/tdnet/` 活用)
- **テーマ強度** (`data/kabutan_themes/`) で当日トップテーマに該当 → mult ×1.3
- **アナリスト格上げ / 業績修正**: external feed が必要 (Phase 3)

### C. 場中観察によるセッション内更新

- **9:30 集計**: 朝寄り完了時点で乖離した銘柄の mult を動的更新
- **11:30 集計**: 前場終値ベースで後場 mult を再計算
- 既存 strategies の `signal` 発生頻度を観察し、ヒット率の高い銘柄を優先

### D. 損失抑制側 (mult 動的縮小)

- **同銘柄での連敗**: 直近 3 連敗以上 → mult ×0.5 (一時 demote)
- **当日累積損失**: 銘柄別に -2% ヒット → mult=0
- **ボラ閾値** : ATR が異常 (>3% / 5min) になったら mult ×0.5

## 実装ロードマップ (Phase 2)

### Step 1: 朝の "動意スコア" 計算 (9:30 cron)

`scripts/d7p2_morning_momentum_score.py` (新規):

- 9:00-9:30 の OHLCV (yfinance / J-Quants 1m or 5m) を全 universe 銘柄で取得
- 当日 ATR 増分、出来高サージ、方向性スコアを計算
- `data/d7p2_morning_momentum_<date>.json` に保存
- 9:30 直後に cron で実行

### Step 2: 動的 lot_multiplier 計算

`scripts/d7p2_compute_dynamic_lot_mult.py`:

- Step 1 のスコアを読み込み、static lot_multiplier に動的補正を乗算
- `dynamic_lot_multiplier = static_lot_multiplier × momentum_factor` (clip [0.3, 4.0])
- `data/lot_multiplier_dynamic_<date>.json` に保存

### Step 3: jp_live_runner で動的 mult 優先読込

```python
@classmethod
def _resolve_lot_multiplier(cls, symbol: str, sid: str) -> float:
    # Phase 2 動的 mult を優先、なければ静的にフォールバック
    dyn_path = Path(f"data/lot_multiplier_dynamic_{today_jst()}.json")
    if dyn_path.exists():
        # 当日朝計算済みの動的 mult を使用
        ...
    return static_lot_multiplier
```

### Step 4: 連敗 / 当日損失による動的縮小

`jp_live_runner` 内に PnL trail を持たせ、リアルタイム mult 縮小を実装:

```python
def _live_mult_adjustment(self, symbol: str, strategy_name: str) -> float:
    # 直近 3 連敗 → 0.5
    # 当日累積 -2% → 0.0
    return adjust_factor
```

これは既存の `_record_skip_event` と並列で `_record_pnl_event` を持たせ、
銘柄 × 戦略単位の loss tracking を実装する。

## ピラミッディング (Phase 2 拡張案)

ユーザー提案: 「**勝ちポジションに追加 entry**」 で利幅を伸ばす。

実装案:

- 既存 `JPMacdRci` には `max_pyramid` パラメータあり (現状 0 デフォルト)
- 勝ちトレード保有中、価格が +0.5% / +1.0% 進む毎に追加 entry
- ロットは初期の 50% / 30% で漸減 (リスク偏重を避ける)
- SL は最終 entry の avg_price - sl_pct で再計算 (= trailing なし、平均値固定)

これは戦略レベル (per-strategy) の改修なので Phase 1 の lot_multiplier とは独立。
有望戦略 (MacdRci 6752/9984) で先行 PoC → 効果確認後に展開。

## リスク管理 (動的化での留意点)

ユーザー方針: 「**ルールだからとロット制限はしない**」。
ただし、**口座保全のための最低限のガードレール** は残す:

- `concurrent_value_cap = 1.5` (信用枠 99万 × 1.5 = 148.5万) は維持
- `lot_multiplier` の絶対上限 = 4.0 (1 銘柄が単独で信用枠 1.2x まで)
- 当日累積損失 -3% 到達 → 全戦略停止 (既存 daily_loss_guard 強化)
- 月次 -5% 到達 → 翌月の lot_multiplier 全体縮小 (×0.5)

## Phase 2 着手判断

- **Phase 1 の効果確認** が先 (5/7 paper 開場後 1 週間運用)
- 効果が +20,000 円/日 以上なら Phase 1 で十分 (目標達成)
- 効果が +10,000 円/日 程度なら Phase 2 で動的化
- Phase 2 開発期間: **3-5 日** (動意スコア計算 + 動的 mult 反映 + 連敗縮小)

## 関連 Issue / Notes

- **当日朝の動意観察** には 1 分足が望ましい → ohlcv_cache 1m 化が前提
  (D5 で TODO リストに残った "1 分解像度 backtest" と並行で 1m パイプ整備)
- **TDnet / kabutan theme** はすでにデータ収集済み (`data/tdnet/`, `data/kabutan_themes/`)
  → Phase 2 でこれらを mult 算出に統合する余地あり

---

**目的**: 「**A 銘柄 80%、B 銘柄 40% の期待値なら B を捨てて A に振る**」 を
当日朝の場況に応じてリアルタイムに実現する。
