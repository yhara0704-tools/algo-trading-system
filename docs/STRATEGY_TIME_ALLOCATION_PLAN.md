# 戦略時間アロケーション設計 — 「場中全ての時間 × 最適な銘柄 × 最適な手法」

## 起点

ユーザー指針 (2026-04-30 17:46):
> 場中全ての時間を最適な銘柄最適な手法でトレードできれば、
> アルゴデイトレードの理論最大値を叩き出せるかもしれないよ。

## 現状の戦略時間カバレッジ (2026-04-30 時点)

```
時間帯              現状           課題
─────────────────────────────────────────────────────────────────────
09:00-09:30  MicroScalp (D6 で確立、+1,675円/日 補助役、29 銘柄)
                              + 既存戦略 (寄り付きシグナル)
09:30-11:30  既存戦略のみ      MicroScalp は擬陽性化で除外 (D2 で確認)
                              ⇒ 戦略密度が低い時間帯
11:30-12:30  [ランチ休止]      東証ザラ場休 — 何もできない
12:30-15:00  MicroScalp 補助   + 既存戦略
                              + Scalp / MacdRci / Pullback / Breakout
15:00-15:25  既存戦略のみ      MicroScalp は eod_close 巻込み防止で除外
                              ⇒ 大引け前のラスト 25 分はほぼノーケア
15:25-15:30  [eod_close]      強制決済
```

## カテゴリ × 戦略 ベースのアロケーション設計 (D7 で確立)

`data/category_strategy_matrix.json` から各カテゴリのチャンピオン戦略:

| カテゴリ | n_銘柄 | チャンピオン戦略 | 期待 oos_daily |
|---|---|---|---|
| A 高ボラ+ショート優位 | 1 (Unitika) | MacdRci | +27,978 円/日 |
| B 高ボラ+順張り | 5 | EnhancedMacdRci | +9,042 円/日 |
| C 中ボラ+順張り | 9 | Pullback | +1,179 円/日 |
| D 中ボラ+中立 | 1 | Scalp | +308 円/日 |
| E 低ボラ+順張り | 18 | MacdRci | +340 円/日 |
| F 低ボラ/NG | 1 | MacdRci | +187 円/日 |

これに **MicroScalp の補完** を加える:

| カテゴリ | MicroScalp 適用判断 | 理由 |
|---|---|---|
| A | ✗ MacdRci に勝てない (+28,000 vs MicroScalp +6,300) | 規格外チャンピオン |
| B | ✗ EnhancedMacdRci に勝てない | 順張り戦略が完全優位 |
| C | △ Pullback と並走価値あり (+1,179 + MicroScalp +1,675) | 補助役 |
| **D** | **◎ Scalp +308 を超えられる**、唯一の niche | 戦略不在ゾーン埋め |
| E | ✗ TP=5 円が届かない (低ボラ) | 構造的不適合 |
| F | ✗ 同上 | 構造的不適合 |

## 時間帯 × カテゴリ × 戦略 マトリクス (将来設計 — M2-M3)

ohlcv_1m 蓄積が 30+ 日達成された段階で、以下のマトリクスを実装:

### 09:00-09:30 (寄り付きボラ捕捉)

| カテゴリ | 主力 | 補助 |
|---|---|---|
| A | MicroScalp_short (寄り天 57.1%) | - |
| B | MicroScalp (両方向) | - |
| C | MicroScalp + Scalp | - |
| D | MicroScalp (中立) | - |
| E | (ボラ不足、エントリーしない) | - |
| F | (除外) | - |

### 09:30-11:30 (前場後半 — 戦略密度の低い時間帯)

| カテゴリ | 主力 | 補助 |
|---|---|---|
| A | MacdRci | Breakout |
| B | EnhancedMacdRci / MacdRci | Breakout / Pullback |
| C | Pullback | MacdRci, Breakout |
| D | Scalp | (要観察) |
| E | MacdRci | Breakout, Scalp |
| F | MacdRci (慎重) | - |

### 12:30-15:00 (後場)

| カテゴリ | 主力 | 補助 |
|---|---|---|
| A | MicroScalp_short + MacdRci | BbShort |
| B | EnhancedMacdRci + MicroScalp | Breakout |
| C | Pullback + MicroScalp_back | Scalp, MacdRci |
| D | Scalp + MicroScalp | (要観察) |
| E | MacdRci + Breakout | Scalp |
| F | MacdRci | - |

### 15:00-15:25 (大引け前ラスト 25 分 — 現状ノーケア)

```
新戦略開発候補:
  - 終値接近スキャル: 大引けに向かう価格収束を取る
  - 大口板突き売買: 引値投資家のフィニッシュ売買を捕捉
  - 引け成り分割発注: VWAP 戻り戦略の 5 分版
```

→ M3 で「ClosingScalp」戦略を新規開発する候補

## 実装ロードマップ

### M1 (現在 = 2026-04-30)
- [x] D2-D6 で「データドリブン銘柄選定」インフラ完成
- [x] D7 でカテゴリ × 戦略マトリクス確立 (本ドキュメント)

### M2 (5 月中旬予定)
- [ ] 1m データ 14-21 日蓄積待ち
- [ ] `scripts/analyze_strategy_time_heatmap.py` 実装
  - jp_trade_executions の 30 日分を時間帯バケットで集計
  - 既存戦略の時間帯別 PnL/WR を可視化
- [ ] `data/strategy_time_allocation.json` 出力 (時間帯 × カテゴリ × 戦略 → 推奨)

### M3 (5 月末予定)
- [ ] 1m データ 30+ 日蓄積完了
- [ ] `jp_live_runner` に時間帯 × カテゴリ別の戦略スイッチ機能を追加
  - `if 時間帯 = "09:30-11:30" and category = "C": active_strategies = ["Pullback", "MacdRci"]`
- [ ] ClosingScalp 戦略の MVP 実装
- [ ] Paper trading で 14 日検証

### M4 (6 月予定)
- [ ] M3 の検証結果を踏まえた本番投入
- [ ] 「**場中 5 時間 × 最適な銘柄 × 最適な手法**」のフル稼働
- [ ] **アルゴデイトレードの理論最大値**へのアプローチ

## 期待値モデル (現状ベンチマーク)

```
現状 (2026-04-30 T1 攻めシフト後):
  daily_target_jpy = 5,000 円/日 (元本 30 万 ROI 1.7%/日)

D7 マトリクス + MicroScalp 補完による上振れ余地:
  + カテゴリ A (Unitika MacdRci): 既存活用、+27,978 円/日 (1 銘柄)
  + カテゴリ B (5 銘柄 EnhancedMacdRci): 平均 +9,042 円/日 × 5 = +45,210 円/日
  + カテゴリ C (9 銘柄 Pullback): 平均 +1,179 × 9 = +10,611 円/日
  + カテゴリ E (15 銘柄 MacdRci): 平均 +340 × 15 = +5,100 円/日
  + MicroScalp 補完 (D-C カテゴリ): +1,675 円/日
  = 合計理論値 +89,774 円/日 (元本 30 万 ROI ~30%/日)

但しこれは「全戦略が同時並走 + 全銘柄が常時シグナル発生」の理論上限。
実際の同時保有制約 (max_concurrent=5) と日内シグナル数で割引。
現実的には +30,000-50,000 円/日 (ROI 10-15%/日) が射程。
```

ユーザー目標 +30,000 円/日 (+3%) は達成可能線、上振れて +50,000 円/日 (+5%) も視野。

## 参照

- `data/category_strategy_matrix.json` — カテゴリ × 戦略の詳細
- `data/symbol_categories.json` — 35 銘柄のカテゴリ分類
- `data/symbol_open_profile_full.json` — 銘柄別プロファイル
- `data/ohlcv_1m/_index.json` — 1m データ蓄積状況
- `docs/IMPLEMENTATION_LOG.md` — D2-D7 の実装履歴
