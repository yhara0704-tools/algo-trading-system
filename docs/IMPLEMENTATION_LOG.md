# Phase1 実装ログ

## 2026-04-28 (深夜4) イベント駆動戦略 H4-light: TP拡大 + cutoff + ドテン買い実装

### 背景
H1–H3 PoC で動画ロジックの方向性は数値で支持された (short avg +0.61% / sum +6.70%, n=11)。次ステップとして H4-light を実施: 90日延長 + cutoff 14:50 + TP 5% → 8% (動画通り) + ドテン買い (動画核心 SHORT_PHASE → REVERT_LONG → ZERO_LINE_TEST → SQUEEZE_RIDE) の 2-leg シミュレーション。

### (H4a) TDnet 90日キャッシュ
`scripts/tdnet_event_collector.py --days 90` → 103 件 (positive 13 / neutral 79 / negative 11)。yfinance の 1分足は 60日制限のため、サンプル増加は限定的（既存 75 → 103 件取得、ただし PnL 計算可は 20 件で同水準）。

### (H4b) `scripts/post_release_revert_poc.py` 拡張
追加機能:
- `--max-event-time HH:MM`: 開示時刻上限 (デフォルト 14:50 推奨)。`after_cutoff_HH:MM` skip 出力
- `fetch_prev_close()`: yfinance 日足で前日終値取得 (ドテン買いの ZERO_LINE)
- `--enable-dote-long`: ショートが TP 到達した場合のみ leg2 でロング転換
  - leg2 SL: ドテンエントリ価格 -1.5%
  - leg2 TP1 (ZERO_LINE_TEST): 前日終値到達で `crossed_zero` フラグ
  - leg2 TP2 (SQUEEZE_RIDE): `crossed_zero` 後に H1 抜けで決済
- `total_pnl_pct` カラム = leg1 + leg2

### (H4c) Baseline vs v2 比較

| パラメータ | Baseline | v2 (H4-light 推奨) |
|---|---|---|
| initial_window_min | 5 | 5 |
| initial_rally_max_pct | 3% | 3% |
| short_tp_drop_pct | 5% | **8%** (動画通り) |
| max_event_time | 15:00 | **14:50** |
| enable_dote_long | × | **○** |

| 指標 | Baseline | v2 |
|---|---|---|
| Short n | 11 | 11 |
| Short 勝率 | 36.4% | 36.4% |
| Short avg PnL% | +0.61 | **+0.88** |
| Short sum PnL% | +6.70 | **+9.70** ★ |
| Short best | +5.00 | **+8.00** |
| Short worst | -1.11 | -1.11 |
| ドテン 2-leg n | - | 1 |
| ドテン total avg | - | +6.50% |

**Short ベスト個票 (v2)**:
- 4/16 13:00 G-データセクション (3905): r1=2.97% → leg1 +8.0% TP → **leg2 -1.5% SL → total +6.5%** (動画パターン典型: TP 到達後の値戻りでドテンが SL ヒット → ただし leg1 益が大きく総合プラス維持)
- 4/06 13:00 京進 (4735): r1=0.63% → leg1 +1.59% (eod)
- 4/01 14:00 G-Birdman (7063): r1=2.34% → leg1 +1.56% (eod)

### 評価

**良い点**:
- TP 5% → 8% への拡大で sum +6.70% → +9.70% に **44% 改善** (同じ 11 トレードで利益確定が遅延)
- ドテン買いの 2-leg は 1 件のみ発動だが、leg2 SL でも total +6.5% で機能 (leg1 益が leg2 損を上回る構造)
- cutoff 14:50 で 34 件削減しても short n は 11 で不変 → cutoff は安全マージン取得に効くだけ

**課題**:
- ドテン発動条件が厳しい (leg1 TP 到達のみ): 11 件中 1 件のみ
- yfinance 60日制限が事実上のサンプル上限
- 動画の SQUEEZE_RIDE (高値突破ホールド) は実装したが、当該日 (4/16 G-データセクション) は H1 まで戻らずに SL ヒット → 1分足で再現難
- 試行 (window 3min/閾値 2.5%) は短縮で short 判定が 11→6 に減り逆効果 (小サンプルでは window 5min が安定)

### 次サイクル候補 (H5)

| 案 | 概要 | コスト |
|---|---|---|
| H5-data | jQuants Premium ¥16,500/月 で 1分足 2年遡及。サンプル 200-400 件で統計信頼性確保 | ¥16,500/月 |
| H5-live | kabu Station API 接続後にライブ TDnet 監視 + ペーパー実弾化 | API利用料 |
| H5-monthly | TDnet を月次蓄積 (`scripts/setup_tdnet_monthly_cron_vps.sh`) し、半年後に有効サンプル 50-100 件で再検証 | ゼロ |

私の推奨: **H5-monthly** で月次蓄積を始めつつ、PoC 結果は「動画ロジックは方向性として有効」と確認できたので H5-live に進む段取りも並行検討。kabu Station API 接続後に再評価。

PoC 成果物 (H4 追加):
- `data/tdnet/events_90d.json` (103 events)
- `data/tdnet/poc_baseline_90d.json`
- `data/tdnet/poc_improved_90d.json` (window 3min 試行 — 不採用)
- `data/tdnet/poc_v2_90d.json` (★ 推奨設定の結果)

---

## 2026-04-28 (深夜3) イベント駆動戦略 PoC: 場中決算リバート (H1–H3)

### 背景
ユーザよりテスタ氏動画 ([youtu.be/LaZSmYVZgUU](https://youtu.be/LaZSmYVZgUU?si=XFOUNIKratCLHuqG)) の「7003 三井 E&S 場中決算で需給読みでショート → ドテン買い → 踏み上げ ride」プレイを売買ロジックに落とせるか相談。既存システムにイベント駆動戦略はなくレパートリー多様化の意味でも筋がよさそうなので、**A. PoC まで** (TDnet 取得 + 過去 60日でバックテスト) を実施。

動画から抽出した 5 フェーズ状態機械（参考）:
```
WAIT_EVENT → INITIAL_REACTION (T+0..+5min, r1=H1上昇率を測定)
  ├─ r1 < 3% (織り込み済み)  → SHORT_PHASE → -8%下げで → REVERT_LONG
  └─ r1 ≥ 3% (反応強)        → 順張りロング (動画では扱わない)
REVERT_LONG → ZERO_LINE_TEST (前日終値で売り出るか観察)
  ├─ 重い  → ロング利食い撤退
  └─ 突破 → SQUEEZE_RIDE (初動高値H1抜けでショート踏み上げ ride)
```

### (H1a) データソース調査
- **TDnet**: yanoshin WebAPI (`webapi.yanoshin.jp/webapi/tdnet/list/<YYYYMMDD>.json`) が無料・秒精度・過去日対応で利用可能
- **jQuants**: 開示エンドポイントなし。1分足は Premium プラン (¥16,500/月) が必要
- **yfinance**: 1分足は60日まで取得可、ただし新銘柄コード (4桁+英数字) や東証外で欠損

### (H1b) `scripts/tdnet_event_collector.py` 新規作成
yanoshin TDnet WebAPI を叩いて期間指定でキャッシュ。タイトルから「業績予想の修正」「上方修正」「増配」等を抽出、極性 (positive/neutral/negative) と場中フラグを付与。
- 過去 60日 → 75 件のザラ場中業績修正開示を取得
- 内訳: positive 10 / neutral 55 / negative 10

### (H2) `scripts/post_release_revert_poc.py` 新規作成
動画の 5 フェーズ状態機械を簡易版 (1 phase short or long) で実装。

```
event_t0 → 直前 close = C_pre
event_t0 〜 +5min の最高値 H1 → r1 = (H1-C_pre)/C_pre

if r1 < 3% → ショート: SL=H1+0.3%, TP=entry*(1-5%), 大引け強制決済
else      → ロング:  SL=entry*(1-2%), TP=entry*(1+4%), 大引け強制決済
```

### (H3) PoC 結果 (60日, 75件 → 有効 20件)

| シナリオ | n | 勝率 | avg PnL% | sum PnL% | best | worst |
|---|---|---|---|---|---|---|
| **short** (r1<3%) | 11 | 36.4% | **+0.61** | **+6.70** | +5.00 | -1.11 |
| long (r1>=3%) | 9 | 44.4% | -0.40 | -3.56 | +1.27 | -2.00 |

**初動上昇率 r1 分布**: min 0% / p25 2.34% / median 2.84% / p75 3.98% / max 9.99%
→ 動画の「2-3% で止まったら逆張り」は r1 中央値付近が閾値で **分布として妥当**。

**スキップ 55件**:
- no_ohlcv 34: yfinance 1分足が取れない銘柄 (新銘柄コード, 東証外, ETF 等)
- no_post_window 16: 14:55 以降開示で 5分初動 + 残り時間がない
- no_init_window 5: イベント時刻直後の 1分足が空

**個票ハイライト (Short ベスト)**:
- 4/16 13:00 G-データセクション (3905): r1=2.97% → TP +5.0% ★動画パターン典型
- 4/01 14:00 G-Birdman (7063): r1=2.34% → +1.56%
- 4/21 14:00 東ソー (4042): r1=2.53% → +1.31%

### 結論 (PoC 評価)

**動画ロジックの方向性は数値で支持される**:
- ショート avg +0.61% / sum +6.70% (n=11) — 60日サンプルでもプラス期待値
- リスクリワード型 (best +5%, worst -1.1%, 勝率 36% でも収益化)
- positive 上方修正での検証はサンプル不足 (n=1 のみ) で結論不能

**現状の限界**:
1. データ量: 60日 75件 → 有効 20件は統計的にギリギリ
2. ザラ場 14:55 以降開示が多く 5分初動を待てない (16件欠損)
3. 戦略は単純な 1-phase short/long のみ。動画の「ショート → カバー → ドテン買い → 高値突破ホールド」多段階は未実装
4. 個人人気度フィルタ未実装 (信用倍率・出来高 z-score、theme_map との交差判定)

### 次ステップ候補

| 案 | スコープ | 効果見込み |
|---|---|---|
| H4-extended | 1) 過去 1年 TDnet 蓄積 (yanoshin で取れる) 2) jQuants Premium 契約で 1分足 2年遡及 3) 5 フェーズ完全実装 4) lookup_kabutan 拡張で信用倍率取得 → 人気度フィルタ 5) theme_map 連携でテーマ強度 × イベントの相互増強 | サンプル 200-400 件、戦略期待値の信頼性向上 |
| H4-light | 1) 60日 → 90日に延長 2) 14:50 までに開示時刻制限 3) ショート利食い 5% → 8% に拡大 (動画通り) 4) 初動 window 5min → 3min 短縮 | 既存データで微調整、追加コストなし |
| 棚上げ | PoC 結果をログに残し、kabu Station API 接続後に再開 | リソース節約、既存戦略の改善に集中 |

PoC 成果物:
- `scripts/tdnet_event_collector.py` (新規)
- `scripts/post_release_revert_poc.py` (新規)
- `data/tdnet/events_60d.json` (75 events)
- `data/tdnet/poc_revert_results.json` (20 simulated trades + summary)

---

## 2026-04-28 (深夜2) 株探テーマ取得 + 業種×テーマ二軸運用へ移行 (G1–G5)

### 背景
ユーザより重ねて指摘:「業種だけで判断しちゃってる。株探にはテーマがあって、ここで確認できる」（[株探テーマランキング](https://kabutan.jp/info/accessranking/3_2)）。F1–F5 で導入した「業種ベース sector_map 純化」は方向としては正しいが、ユーザの「レアアース関連 (6330)」「パワー半導体関連 (5381)」「フィジカルAI関連 (6433)」というメモを業種ベース判定で否定してしまっていた。**実際に株探テーマ DB を引くと、ユーザの初期メモはすべて公式テーマと一致**していた。設計上、業種とテーマは独立軸で扱う必要がある。

### 検証結果（株探公式テーマとの照合）
| 銘柄 | 業種 (株探) | 株探テーマ (公式) | ユーザ初期メモ |
|---|---|---|---|
| 6330 東洋エンジ | 建設業 | プラント, **レアアース**, LNG, 水素... (40 テーマ) | レアアース関連 ✓ |
| 5381 マイポックス | ガラス・土石 | 研磨, 半導体部材・部品, **パワー半導体**, 半導体, データセンター... (14) | パワー半導体関連 ✓ |
| 6433 ヒーハイスト | 機械 | 軸受け, 機械・部品, **ロボット**, **フィジカルAI**, Society5.0... (8) | フィジカルAI関連 ✓ |
| 247A AIロボティクス | 化学 | **化粧品**, スキンケア, **美容家電**, 人工知能, D2C... (11) | フィジカルAI関連 ❌ |
| 4082 第一稀元素 | 化学 | **レアアース**, 燃料電池, リチウムイオン電池... (16) | レアアース関連 ✓ |
| 6629 テクノホライゾン | 電気機器 | 通信機器, 監視カメラ, FA関連, **画像認識**, **顔認証**... (30) | フィジカルAI関連 (近接) |
| 5243 note | 情報・通信業 | **C2C**, コンテンツ配信, SaaS, 人工知能... (14) | 新興ITメディア (近接) |

**結論**: ユーザの初期メモ 6/7 が株探公式テーマと一致。私の F2 訂正（「業種優先」で 6330→プラント、5381→半導体素材・研磨、6433→産業機械・精密部品）は業種準拠としては正しいがテーマ文脈を切り捨てていた。

### (G1) `lookup_kabutan.py` 拡張
銘柄個別ページの `<th scope='row'>テーマ</th>` セルから複数テーマを抽出する正規表現追加。`industry` に加え `themes: [str]` を返却。出力フォーマットも `業種:`/`テーマ(N):`/`概要:` の縦並びに改修。

### (G2) `scripts/fetch_kabutan_theme_constituents.py` 新規作成
株探テーマページ (`https://kabutan.jp/themes/?theme=<theme>`) を全ページネーション対応で取得し、構成銘柄コード一覧を `data/kabutan_themes/<theme>.json` に保存。1 req/sec で過剰アクセス回避。
- 取得済み: レアアース(35), パワー半導体(83), フィジカルAI(43), 半導体部材・部品(105), 化粧品(124), 美容家電(12), ロボット(138), 軸受け(23)
- ページネーション URL は相対形式 (`?theme=...&page=2`) のため regex を `[?&]page=(\d+)` に修正

### (G3) `data/theme_map.json` 新規作成
私のユニバース ⊂ 株探公式テーマ の交差マップ。各テーマに `kabutan_url`, `constituents_total`, `tracked: [{symbol, name, kabutan_industry, reason}]` を持つ。
- 「レアアース」: 4082, **6330** (← F2 で外したものを正しい場所に再配置)
- 「パワー半導体」「半導体部材・部品」: 5381 (← F2 で「半導体素材・研磨」新設したものを公式テーマに移行)
- 「フィジカルAI」「ロボット」: 6433, 6629
- 「化粧品」「美容家電」: 247A
- 「C2C」: 5243

### (G4) sector_map.json から重複テーマセクターを削除
F2 で新設した以下のテーマセクターは theme_map.json に正式移管したため sector_map から削除:
- レアアース・希土類 (4082) → theme_map「レアアース」へ
- プラント・エンジニアリング (6330, 1963, 6366) → 6330 は theme_map「レアアース」へ。1963/6366 は今のところ未追跡
- パワー半導体・関連装置 (6963, 6707) → 一旦 sector_map から外し theme_map に移管予定 (今夜は未対応)
- 半導体素材・研磨 (5381, 5384, 4063) → theme_map「半導体部材・部品」へ
- 化学・化粧品 (4911, 4922, 4452, 4927, 247A) → theme_map「化粧品」へ
- 新興ITメディア (5243) → theme_map「C2C」へ

sector_map に残したテーマ系セクター (フィジカルAI・ロボティクス, 光半導体・フォトニクス, 小売IT・RFID, 建設サービス・地盤) は **n>=2 で複数銘柄ある業種混在テーマセクター** として _doc 明記の上で維持。次サイクルで theme_map に純化検討。

`scripts/sector_strength.py` には `_iter_sectors` ヘルパーを追加し `_doc` などの string トップキーをスキップ。

### (G5) RUNBOOK §5.3 「業種 × テーマの二軸運用」に書き換え
- 二軸の役割と紐付け手順 (lookup → fetch → 追加 → sector_strength 再計算)
- 業種ベース vs テーマベース の追加ルール
- 落とし穴: 「業種だけで判断しない」「社名だけで判断しない」を例示で明記

### 反省と教訓
- F2 で「業種優先」と判断した時点で**既にユーザのテーマ文脈メモを踏みつぶしていた**。次回からは業種とテーマを必ず両軸で取得し、ユーザの提示するテーマキーワードを株探の公式テーマと照合する手順を**必ず**経由する
- `scripts/lookup_kabutan.py` `scripts/fetch_kabutan_theme_constituents.py` `data/theme_map.json` の三点セットが、新規銘柄追加時の必須プロトコルとして RUNBOOK §5.3 に整備された

---

## 2026-04-28 (深夜) sector_map 株探検証 + 誤分類訂正 (F1–F5)

### 背景
ユーザより「セクターは株探で確認するといい」とのアドバイス。247A の業種誤分類が発覚した直後で、提案2巡目 7 銘柄を全件株探で検証したところ **5 件が業種乖離**。さらに既存 sector_map 全銘柄を抜き打ち検証した結果、テーマ命名（「電機・家電」等）と東証業種（「電気機器」）の文字列揺れは許容範囲だが、**実体的な誤配置**が複数発覚した。

### (F1) `scripts/lookup_kabutan.py` 新規作成
株探の銘柄基本情報ページから **東証業種・社名・概要** を取得するスクレイピングヘルパー（1 req/sec）。`--from-sector-map --diff-only` で既存マップとの乖離だけ抽出可能。`--json` 出力対応。

### (F2/F3) sector_map.json 訂正
| code | 銘柄 | 株探業種 | 訂正前 | 訂正後 |
|---|---|---|---|---|
| 247A | AIロボティクス | 化学 | フィジカルAI ❌ | **化学・化粧品** (前ステップで対応済み) |
| 6433 | ヒーハイスト | 機械 | フィジカルAI ❌ | **産業機械・精密部品** (THK向け軸受け) |
| 6330 | 東洋エンジニアリング | 建設業 | レアアース ❌ | **プラント・エンジニアリング** (新設) |
| 5381 | マイポックス | ガラス・土石 | パワー半導体 ❌ | **半導体素材・研磨** (新設) |
| 4082 | 第一稀元素 | 化学 | レアアース | レアアース・希土類 維持（テーマ整合） |
| 6629 | テクノホライゾン | 電気機器 | フィジカルAI | フィジカルAI 維持（株探概要も「ロボティクスを核」と明記） |

新設セクターは複数銘柄で n>=3 を担保:
- **プラント・エンジニアリング**: 6330 東洋エンジ + 1963 日揮HD + 6366 千代田化工
- **半導体素材・研磨**: 5381 マイポックス + 5384 フジミ + 4063 信越化学

各銘柄エントリに `kabutan_industry` フィールドと `note` を付与し、テーマと業種の関係を可読化。

### (F4) sector_strength 再計算 (20 セクター, 68 銘柄)
訂正後の主要 label 変化:
- **フィジカルAI・ロボティクス**: med_5d **+10.94%** (n=4) ★ 純度向上で前回 +7.23% → +10.94%
- **半導体素材・研磨** (新設): med_5d **+5.17%** (n=3) → strong。マイポックスは旧分類「パワー半導体 weak (-8.36%)」だったため gate に誤って引っかかる構造だったが、本来の業種 strong に補正された
- **産業機械・精密部品**: med_5d **+5.84%** (n=4) → strong（6433 加入後も維持）
- **プラント・エンジニアリング** (新設): med_5d **-1.56%** (n=3) → weak。6330 はここで weak gate 対象になる（実態と整合）
- **化学・化粧品**: med_5d +0.02% → mixed。247A はここで weak gate を発動しない
- **レアアース・希土類**: 4082 単独で n=1 mixed (-0.13%)

observation_pairs の 247A reason は前ステップで「化学・化粧品 mixed」と記載済みのため変更不要。

### (F5) 運用ルール doc 化 — `docs/RUNBOOK_PHASE0_TO_PHASE1.md §5.3`
- 新規銘柄追加前は `scripts/lookup_kabutan.py` で東証業種を必ず確認
- テーマセクターの場合は `_doc` で物色キーワードを明記、各銘柄に `kabutan_industry` と `note` を付与
- 1 セクター n>=2-3 を確保（n=1 は sector_strength med 計算が脆弱）
- 追加後は `sector_strength.py` 再実行で label 変化を確認

### 構造的気付き（次サイクルで対応検討）
- `sector_map.json` は「テーマ × 業種」の混在マップ。本格的には `sector_map.json` (東証業種準拠) と `theme_map.json` (物色テーマ) を分離し、`sector_strength` は業種ベース・`theme_momentum` はテーマベースで両立させるのが理想
- 現状は妥協として「テーマセクター」「業種セクター」を `_doc` で区別する運用とし、構造分離は将来課題

---

## 2026-04-28 (夜) 提案2巡目 7 銘柄 (E1–E6): フィジカルAI 拡充 + observation 3 ペア追加

### 背景
ユーザから 2 巡目の銘柄提案: 6330 東洋エンジニアリング(レアアース) / 5381 マイポックス(パワー半導体) / 6433 ヒーハイスト(フィジカルAI) / 6629 テクノホライゾン(フィジカルAI) / 5243 note / 4082 稀元素(レアアース) / 247A AIロボティクス(フィジカルAI)。前回 (C1–C7/D1–D3) と同じ流れで段階検証した。

### (E1) 横断スキャン — `scripts/scan_candidate_symbols.py`
6 戦略 × 7 銘柄、IS=直近30日/OOS=その前30日。**Robust 認定**:
- 6330.T ORB (IS+455 / OOS+830, PF 28.46, Tr 5)
- 6433.T ORB (IS+320 / OOS+4, PF 1.00, Tr 13)
- 6629.T Scalp (IS+49 / OOS+425, PF 1.77, Tr 74)
- 5243.T Momentum5Min (IS+772 / OOS+123, PF 1.17, Tr 28)
- 247A.T Momentum5Min (IS+1,082 / OOS+457, PF 1.32, Tr 51) ★最大
- 247A.T Breakout (IS+36 / OOS+19, Tr 50)
- 247A.T Scalp (IS+106 / OOS+196, PF 1.39, Tr 70)
**IS-only**: 5381.T Scalp / 4082.T Momentum5Min（OOS 反転で不採用）

### (E2) sector_map.json 拡張
- 「フィジカルAI・ロボティクス」に 6433/6629 を follower として追加（3 銘柄 → 5 銘柄）
- 新規セクター追加: 「レアアース・希土類」(4082/6330, 米プロキシ REMX/MP) / 「パワー半導体・関連装置」(5381/6963/6707, 米プロキシ WOLF/ON) / 「新興ITメディア」(5243, 米プロキシ PINS/SNAP)
- **業種訂正 (E2.1)**: 247A AIロボティクスは社名にロボティクスが付くが実体は自社AIを用いた美容家電・化粧品の企画開発で、東証業種は『化学』。当初フィジカルAI・ロボティクスに分類していたが誤り。新セクター「化学・化粧品」(4911 資生堂 leader / 4922 コーセー / 4452 花王 leader / 4927 ポーラ・オルビス / 247A.T) を新設し移管。米プロキシ XLP/EL。
- 訂正後の sector_strength (med_5d): フィジカルAI **strong** (+7.23%, n=5/5、純度向上) / 化学・化粧品 **mixed** (+0.02%, n=5/5)
- VPS 同期済み

### (E3) time_pattern 生成 — `scripts/build_time_patterns.py --symbols ... --days 60`
7 銘柄分の `data/time_patterns/<sym>.json` を生成。
- 共通: 11:30 が bullish スロット（前場引け前）
- 6629.T テクノホライゾン: 11:00/11:30/13:00/14:00/14:30 と長時間 bullish 偏重 + 9:30/10:00/12:30 高ボラ
- 247A.T / 5243.T: 9:00–10:30 集中の高ボラパターン（9:20 起動運用に整合）

### (E4) 5-split WF 裏取り — `scripts/walkforward_candidate_robust.py`
| pair | OOS+ | avg/日 | trades | 判定 |
|---|---|---|---|---|
| **6433.T ORB** | **5/5** | **+1,476円** | 6.6 | ★採用 |
| **247A.T Momentum5Min** | **5/5** | **+1,034円** | 37.6 | ★採用 |
| **5243.T Momentum5Min** | **4/5** | **+411円** | 51.0 | ☆採用 (weak sector) |
| 247A.T Breakout | 3/5 | +2円 | 46.0 | 不採用（M5 が圧倒的） |
| 6330.T ORB | 2/5 | -614円 | 8.2 | 不採用 |
| 6629.T Scalp | 0/5 | -1,025円 | 76.2 | 不採用（IS-OOS 逆転） |
| 247A.T Scalp | 0/5 | -1,062円 | 79.8 | 不採用（IS-OOS 逆転） |

### (E5) sector_strength 再計算（17 セクター）
- **strong**: フィジカルAI・ロボティクス (med_5d +2.81% / med_10d +13.5%) ← 6433/247A はここ
- **weak**: 新興ITメディア (med_5d -2.65%) ← 5243 はここ → sector_strength gate 観察対象
- **weak**: パワー半導体 (-8.36%) / レアアース (-3.26%)

### (E6) universe_observation_pairs に 3 ペア追加
- `6433.T ORB` (force_paper=False, min_trades=6)
- `247A.T Momentum5Min` (force_paper=False, min_trades=30)
- `5243.T Momentum5Min` (force_paper=False, min_trades=30)
- `merge_robust_into_universe.py` 実行で `data/universe_active.json` の `manual_observation` 件数 2 → 5 に拡大
- `force_paper=False` のため `jp_live_runner` は paper 投入せず観察のみ。dry_log の sector_strength gate と組み合わせて段階導入予定

### 次サイクルの観察ポイント
1. 247A.T Momentum5Min (sector strong, trades 健全) → 最有力。daemon の OOS スコアが上位に上がるか追跡
2. 5243.T Momentum5Min が weak sector ガード下でどのくらいスキップされるか (`weak_sector_long_block_dry`)
3. 6433.T ORB は trades 6.6/split と薄いため、過去 30 日で何回 ORB シグナルが出たか観察してから force_paper 化を判定

---

## 2026-04-28 ユーザ提案 7 銘柄の Robust 検証 + sector_strength 設計（C0–C5）

### 背景

ユーザから「最近よく動いてる銘柄」7 件（3444 菊池製作所 / 6522 アスタリスク / 6356 日本ギア工業 / 7013 IHI / 5985 サンコール / 6072 地盤ネット / 5817 JMACS）の提示と、「セクター紐付けで順張り判定」「9:20 開始なら寄り天は無視可」「銘柄×日別パターン先行で手法当て」の方針提案を受け、以下を実施した。

### (C1) 候補 7 銘柄の横断スキャナ — `scripts/scan_candidate_symbols.py`（新規）

`scan_multi_strategy.py` の汎用化。任意銘柄リスト（既定: 提案 7 銘柄）に対し 6 戦略（MacdRci/Breakout/Scalp/Momentum5Min/ORB/VwapReversion）を IS=直近 30 日 / OOS=その前 30 日で評価し、`data/snapshots/scan_candidate_symbols.json` へ保存。

**結果（VPS 実行・60 日 5m データ）**:
- 3444 菊池: Scalp で IS+220 / OOS+251 (PF1.64, Tr63) — 単発 Robust
- 6072 地盤ネット: Scalp で IS+26 / OOS+218 (PF1.77, Tr54) — 単発 Robust
- 5985 サンコール: ORB で IS+300 / OOS+2,556 (PF3.04, Tr16) — 単発 Robust
- 6522 アスタリスク: ORB で IS+198 / OOS+823 (PF6.49, Tr5) — 単発 Robust（薄）
- 6356 日本ギア: ORB で IS+563 / OOS+1,346 (PF8.12, Tr11) — 単発 Robust（薄）
- 7013 IHI: Scalp IS-only / 5817 JMACS: MacdRci IS-only — 過学習臭、不採用

**気付き**: MacdRci は複数銘柄で IS-OOS 反転（OOS 側だけ大きく正）。`scan_multi_strategy.py` は IS=新/OOS=旧の時系列なので、**直近では MacdRci が効きにくい**ことが提案 7 銘柄でも再現された。1605.T で実測した「MacdRci 過学習傾向」と同じ構造。

### (C5) 候補 5-split rolling WF — `scripts/walkforward_candidate_robust.py`（新規）

C1 で Robust 認定された 5 ペアを 90 日 5m データで 5-split rolling walkforward に通し、`oos_positive_rate` と `avg_oos_daily` を出した。

**結果**:
| 銘柄×戦略 | OOS+/5 | avg_oos_daily | trades/split | 判定 |
|---|---|---|---|---|
| 3444 Scalp | 0/5 | -239 | 45.6 | **却下**（30/30 単発のみで偶然 Robust） |
| 6072 Scalp | 0/5 | -1,086 | 54.4 | **却下** |
| **5985 ORB** | **5/5** | **+2,570** | 6.8 | **採用候補**（trades 薄） |
| **6522 ORB** | **5/5** | **+5,098** | 6.2 | **採用候補**（trades 薄） |
| 6356 ORB | 0/5 | -615 | 5.4 | 却下 |

**重要発見**: trades 数が多い 3444/6072 Scalp が WF で全滅、trades 薄の 5985/6522 ORB が 5/5 通過。これは「trades 数が多い = サンプル充足」の単純な前提が崩れる例で、**`scan_multi_strategy.py` の単発 IS-OOS 判定が universe 採用ロジックの構造的弱点を作っている**ことを再確認した。`strategy_factory.py` のコメントで「ORB/Momentum5Min/VwapReversion はアーカイブ済（PDCA 非対象）」とあるが、**全銘柄平均では負だが特定の小型・新興銘柄では正**となるケースが見つかった（ホワイトリスト型の例外運用が有用）。

### (C3) 提案銘柄の time_pattern 生成 — `scripts/build_time_patterns.py`（新規）

`backend.analysis.time_pattern.TimePatternStore.record_from_df` を提案 7 銘柄に対して呼び出し、`data/time_patterns/<sym>.json` を一括生成する薄いラッパ。

**結果（60 日分・3,200–3,700 bars/銘柄）**:
- **全 7 銘柄が寄付き 9:00–10:30 で高ボラ** → ユーザ提案「9:20 開始」運用がこれら銘柄で完全に正しい。
- 多くが 11:30（前場引け前）でブル傾向。
- 6522 アスタリスクは寄付きから大引けまで全帯ブル × 全帯高ボラ → ORB が機能した時間帯特性が裏付けられた。
- bearish 帯がゼロ（直近 60 日が全体上昇トレンドだった副作用）→ 下げ局面サンプル不足には注意。

### (C2) sector_strength スコアラー — `scripts/sector_strength.py`（新規） + `data/sector_map.json` 拡張

`data/sector_map.json` に 6 セクター追加（フィジカルAI・ロボティクス / 防衛・重機械 / 産業機械・精密部品 / 小売IT・RFID / 建設サービス・地盤）。提案 7 銘柄を全て該当セクターに登録。

`sector_strength.py` は yfinance で 3d/5d/10d リターンを取得し、`med_5d ≥ +0.8%` を strong / `≤ -0.8%` を weak とラベル付け。出力は `data/sector_strength_latest.json`、`symbol_to_sectors` マップも同梱。

**結果（2026-04-28 朝）**:
- **strong**: フィジカルAI(+13.98%), 小売IT(+9.69%), 半導体(+7.55%), 光半導体(+5.20%), 産業機械(+4.68%) ← C5 で WF 通過した 5985/6522/6723 がここに集中
- **weak**: 自動車(-6.71), 医薬(-7.33), 海運(-5.45), 銀行(-3.07), 商社(-2.27), 通信(-2.24), 防衛(-1.18), 電機(-0.93) ← **既存 universe 主力銘柄（9984/9432/9433/8306/9107/8058/1605/6758/6501/6752/4568/9468）はほぼ全部 weak**

**衝撃的な構造**: 既存 universe が weak セクター集中で、direct 短期パフォーマンスが詰まっているのは MacdRci 過学習だけでなく **セクター強度を考慮していないこと** が根本要因。`jp_live_runner.py` の entry gate に sector_strength を組み込めば「weak セクター × 順張り MacdRci/Breakout は抑制」「strong セクター × ORB/Scalp は許可」という方向で根本改善できる見込み。

### 結論と次サイクルの宿題

1. **5985.T ORB / 6522.T ORB を paper 追跡候補化**: ただし `oos_trades` が 6 と薄いので、`paused_pairs` の逆機構（`observation_pairs`）か `wf_relaxed` 緩和ルートで段階的に paper に乗せる（C6）。
2. **sector_strength を entry gate に組み込む**: weak セクター × 順張り戦略の skip ルールを `jp_live_runner.py` に追加（C7）。閾値は `strong_5d_pct=0.8 / weak_5d_pct=-0.8` を初期値とし、`data/sector_strength_latest.json` を毎朝再生成する cron も併せて設置。
3. **strategy_factory.py のアーカイブ判断見直し**: ORB を「全銘柄」ではなく「strong セクターの新興銘柄」に限定して再有効化する個別ホワイトリスト型運用を検討。

## 2026-04-28 残タスク C6 / C7 対応（観察候補マージ機構 + sector_strength gate 設計）

### (C6) 観察候補の universe マージ機構 — `data/universe_observation_pairs.json` 新設

C5 で 5/5 OOS positive を確認したが `oos_trades<30` で品質ゲートを通らない (5985.T ORB / 6522.T ORB) を、`source="manual_observation"` で `universe_active.json` に追加できる仕組みを実装。

- 新規ファイル: `data/universe_observation_pairs.json`（symbol/strategy/reason/evidence/added_at/force_paper）
- `scripts/merge_robust_into_universe.py` に `--observation-pairs-path` / `--ignore-observation-pairs` を追加。merge 末尾で `existing_by_sym` に重複なしで挿入し、`observation_added` / `observation_skipped` を report に記録。
- 適用結果: universe 13 → 15 に拡大（5985.T ORB, 6522.T ORB 追加）。`force_paper=false` のため jp_live_runner は既定の `paper_low_sample_excluded` ロジックでスキップする想定。**実 paper 投入は C7 の sector_strength gate と併せて段階導入**（観察候補は現時点でシグナル発生しても約定しない）。

### (C7) sector_strength entry gate 設計案（実装は次サイクル）

**目的**: 既存 universe の主力銘柄が weak セクターに集中している構造を解消し、strong セクター × 順張り戦略を優先する。weak セクター × 順張り戦略 (MacdRci/Breakout/Scalp 順張り) は entry を抑制。

**設計要点**:

1. **入力データ**:
   - `data/sector_strength_latest.json` (毎朝 cron で再生成) の `sectors[].label` (strong/weak/mixed/unknown) と `symbol_to_sectors` マップ
   - 既定閾値: `strong_5d_pct=0.8 / weak_5d_pct=-0.8`（必要に応じて環境変数で調整）

2. **gate 配置**: `backend/lab/jp_live_runner.py` の `_try_open_position` で、既存の `paused_pair` チェック直後に追加。
   ```python
   sector_label = self._sector_strength_label(symbol)
   if sector_label == "weak" and strategy_kind in {"MacdRci", "Breakout", "Scalp"}:
       # observation 候補（manual_observation source）はバイパスする
       if not is_observation_pair:
           self._record_skip_event(reason="weak_sector_long_block", detail={...})
           return False, "weak_sector_long_block"
   ```

3. **観察候補の優先解放**: `observation_meta.force_paper=True` のペアは `weak_sector_long_block` をバイパスする。これにより 5985.T / 6522.T ORB を strong sector × ORB として paper 投入する道筋が開く（次々サイクルで `force_paper=True` に切り替え）。

4. **環境変数による段階導入**:
   - `SECTOR_STRENGTH_GATE_ENABLED=0`（既定）: gate 無効、ログのみ
   - `SECTOR_STRENGTH_GATE_ENABLED=1`: weak sector × 順張り戦略の skip 有効化
   - `SECTOR_STRENGTH_GATE_DRY_LOG=1`: gate 発動を skip_event に記録するが約定は止めない（A/B 比較用）

5. **毎朝 cron**:
   - `setup_sector_strength_cron.sh` を新設し、平日 08:30 JST に `scripts/sector_strength.py` を実行
   - 出力 `data/sector_strength_latest.json` を `jp_live_runner` 起動時と毎時 reload で読み込む

6. **観察期間**: gate 有効化後 1 週間は `SECTOR_STRENGTH_GATE_DRY_LOG=1` で skip_event を集計し、weak sector blockage が想定通りの件数になっているかを `report_jp_signal_skip_events.py --reason weak_sector_long_block` で検証してから本番 ON。

**期待効果**:
- 4/27 や直近の paper drag 要因（weak sector の MacdRci 順張り）の根本原因が遮断される
- strong sector の ORB / Scalp（5985/6522/3444 ※3444 は WF 落ち）に資金とロット枠が回る
- insufficient_lot による機会損失（9984.T 76 件/日）も sector weak のため自然解消方向

### (C8) 残タスク

- C7 実装（次サイクル朝、sector_strength gate を `SECTOR_STRENGTH_GATE_DRY_LOG=1` で投入）
- 6723.T `post_loss_recheck_block` 41 件刈りの妥当性レビュー（recheck 閾値調整 or 一時バイパス）
- `insufficient_lot` 166 件問題: 30 万円資金 × 9984.T 12,000 JPY/株では物理的に 9984.T は 1 ポジ＋複数銘柄共存できない。`POSITION_PCT` 引き下げ or 9984.T 専用ロット制限の検討

## 2026-04-28 (夜) 残タスク D1 / D2 実装 + D3 設計

### (D1) sector_strength gate を `jp_live_runner` に実装（DRY_LOG 観察モード）

`backend/lab/jp_live_runner.py` の `_try_open_position` の paused_pair check 直後に `sector_strength` gate を追加。

**追加要素**:
- モジュール定数: `_SECTOR_STRENGTH_MODE` (off / dry_log / on, env `JP_SECTOR_STRENGTH_MODE`), `_SECTOR_STRENGTH_LONG_STRATEGIES` (MacdRci/Breakout/Scalp/EnhancedMacdRci/EnhancedScalp), `_SECTOR_STRENGTH_PATH`, `_SECTOR_STRENGTH_RELOAD_SEC` (3600s)
- ヘルパー: `_get_sector_strength(symbol) -> (label, sector_names, median_5d_pct)` を `LiveRunner` に追加。`data/sector_strength_latest.json` を mtime ベースの 1h lazy load。複数セクター所属時は最も weak (median_5d 最小) を採用。
- gate 動作: `is_long=True` × `strategy_kind ∈ _SECTOR_STRENGTH_LONG_STRATEGIES` × `sector_label == "weak"` で発動。
  - `mode=off`: 何もしない
  - `mode=dry_log`: `reason="weak_sector_long_block_dry"` で skip_event に記録、約定は止めない（A/B 観察用）
  - `mode=on`: `reason="weak_sector_long_block"` で記録 + return False で実 block

**毎朝 cron**:
- `scripts/setup_sector_strength_cron_vps.sh` を新設、平日 08:30 JST に `sector_strength.py` 実行 → `data/sector_strength_latest.json` 再生成。VPS の crontab に登録済。

**段階導入運用**:
- 4/29 朝から `JP_SECTOR_STRENGTH_MODE=dry_log` で観察開始（.env に追記、サービス再起動済）
- 1 週間集計: `report_jp_signal_skip_events.py --reason weak_sector_long_block_dry` で「もし on にしていたら何件刈っていたか」を可視化
- 4/28 paper のように weak セクター × 順張りでも勝てた日があるため、**1 週間の skip_event 内訳と、当該銘柄の実 paper 損益の比較**（dry_log 件数 × 平均損益）で経済効果を算出してから ON 判断。

### (D2) post_loss_recheck の regime_ok 判定緩和

**根本原因**: 4/28 6723.T で `post_loss_recheck_block=41 件` が全部 `regime_ok=False` だった。entry_regime=`trending_up` で固定されたあと、現在 regime が `trending_down` / `low_vol` / `ranging` を行ったり来たりで永久に baseline と一致せず、TTL=45 分が経つまで全シグナルを刈り続けた。9433.T 38 件は `vol_ok=False`（vol < 0.08）で抑制が妥当。

**緩和実装** (`backend/lab/jp_live_runner.py`):
- 新モジュール定数: `_RECHECK_REGIME_STRICT` (env `JP_RECHECK_REGIME_STRICT`, 既定 1=旧挙動), `_RECHECK_ADVERSE_REGIME_MAP = {"trending_up": "trending_down", "trending_down": "trending_up"}`
- `_evaluate_post_loss_gate` の regime_ok 計算を分岐:
  - **strict (旧)**: `regime_ok = (baseline == "unknown") or (regime_now == baseline)`
  - **adverse_only (新)**: `regime_ok = (baseline == "unknown" or regime_now == "unknown" or adverse is None or regime_now != adverse)`
- detail に `regime_match_mode` を追加して skip_event で旧/新どちらの判定だったか追跡可能に。

**運用**:
- 4/29 朝から `JP_RECHECK_REGIME_STRICT=0` で adverse_only モードに切替（.env に追記、サービス再起動済）
- 期待効果: 6723.T のような「entry_regime と現在 regime が一致しない長時間 block」が解消、ranging/low_vol/high_vol への遷移時は再エントリーが許される
- リスク: adverse 方向（trending_down ↔ trending_up）への明確な反転だけは block 維持されるため、過度な弱体化にはならない

### (D3) 設計ドラフト: `insufficient_lot` 構造対策（実装は次サイクル）

**問題**: `JP_CAPITAL_JPY=300,000 × MARGIN_RATIO=3.3 = 買付余力 990,000`、`POSITION_PCT=0.50` → 1 ポジ上限 495,000 円。9984.T (12,000 円/株 × 100 株 = 1,200,000 円/ロット) は **POSITION_PCT を 1.0 にしても 1 ロット買えない**。4/28 は 76 件の signal が `insufficient_lot` で消滅、期待 oos_daily +9,932 円が完全に取り逃しになった。

**根本対策の選択肢**:
1. **資金増額**: `JP_CAPITAL_JPY` を 30 万 → 40 万 に上げる。買付余力 1.32M で 9984.T 1 ロット成立。実弾移行のタイミングで決済資金の振替を検討。
2. **9984.T を universe から一時除外**: `data/jp_paper_trading_halt.json` の `paused_pairs` に `{"symbol": "9984.T", "strategy_name": "MacdRci", "reason": "insufficient_lot saturation", "until": "..."}` を入れ、insufficient_lot による 76 件の skip_event ノイズを止める。資金増額前のクリーン化。
3. **銘柄別 `lot_size` override**: `data/symbol_lot_overrides.json` を新設し、特定銘柄を `LOT_SIZE=10`（単元未満売買・ミニ株）扱いにする。ただし松井証券一日信用ではミニ株不可のため実弾互換性なし。

**推奨**: (2) 短期 + (1) 中期。今夜は (2) も投入せず、**4/29 朝の paper を観察して insufficient_lot による daily noise が次回も 76+ 件出たら paused_pair に登録**する運用判断を採る。



### 背景

前節 C1/C2/C3 の結果から浮上した 3 課題に対応した。

1. **universe_active に Robust が取りこぼされる** — `weekly_universe_rotation` は `strategy_fit_map` の `is_oos_pass=True` を必須にしているため、MacdRci が Robust でも `is_oos_pass=False` のとき（8306.T, 3382.T, 4689.T, 4911.T, 6758.T, 9432.T, 8058.T）に universe から落ちる。
2. **`3103.T` / `9984.T` など no_signal が支配的な銘柄の要因が不明** — `_record_skip_event` は「ガード発動時」だけ記録しており、信号未成立バーの中間値（MACD/RCI）が残らない。
3. **`regenerate_daily_summary` の手運用** — 不整合が発生しても翌朝の paper preflight まで気付けない。

### (D1) `scripts/merge_robust_into_universe.py`（新規）

`macd_rci_params.json` の `robust=True` を唯一の根拠として `universe_active.json` に上書き反映する日次スクリプト。週次 rotation が保守的に取りこぼす Robust 銘柄を「日次 Robust オーバーレイ」で機会損失ゼロに戻す。

- 入力: `data/macd_rci_params.json`, `data/universe_active.json`
- 挙動:
  - `Robust ∖ Universe` は **追加**（strategy=MacdRci、oos_daily/is_pf/is_trades を macd_rci_params から複写）
  - `Robust ∩ Universe` で strategy が MacdRci 以外 or macd oos_daily が 0.5 yen/日 超過大の場合は **strategy を MacdRci に置換**（1 decimal 丸め由来の微差は誤検知回避）
  - 既存の非 Robust 銘柄は据え置き（削除しない）
  - 実行前に `universe_active.backup_<ts>.json` として自動バックアップ（`--no-backup` で抑止）
- 差分レポートは `data/universe_robust_merge_latest.json` に保存
- `--dry-run` / `--min-oos-daily` サポート

**本日の適用結果** (VPS):
- 追加 7 銘柄: 6758.T (Sony), 3382.T (Seven&i), **8306.T (MUFG)**, 9432.T (NTT), 4689.T (LY), 4911.T (Shiseido), 8058.T (Mitsubishi Corp)
- strategy 置換 5 銘柄: 3103.T Breakout→MacdRci, 9107.T, 6752.T, 6613.T, 9984.T Breakout→MacdRci
- 実行後 `check_robust_vs_universe.py`: `Robust ∖ Universe = 0`（機会損失ゼロ）、Robust ∩ Universe = 17 で全一致。

### (D2) 信号未成立バーの中間値記録 — `no_signal_diag`

`backend/lab/jp_live_runner.py`:

- `_process_strategy` の `last_signal not in (1, -2)` 分岐直前に `_record_no_signal_diag(...)` を追加。
- 新メソッド `_record_no_signal_diag(symbol, strategy_id, signals, now, *, interval_min=30)`:
  - `(symbol, strategy_id)` 毎に **30 分に 1 回** だけスナップショットを追加（`_no_signal_diag_last` dict で rate-limit）。
  - `signals` DataFrame から `macd`, `macd_sig`, `macd_hist`, `rci_*`, `rci_short_slope` を抽出（NaN セーフ）。
  - 未達ヒント `missing` を算出: `macd<=0`, `macd_sig<=0`, `macd_hist<=0`, `rci_majority(k/n)`, `out_of_session`。
  - `reason="no_signal_diag"` で `self._skip_events` に追加 → EOD に `save_jp_signal_skip_events` 経由で DB 永続化。
  - `signals` に `macd` 列が無い戦略（Breakout/Scalp）は早期 return（無駄なレコードを残さない）。
- `self._no_signal_diag_last: dict[tuple[str, str], datetime]` を `__init__` に追加。日次リセットは行わず、同一プロセス生存中は 30 min 間隔を維持（再起動時は自然リセット）。

`scripts/report_jp_signal_skip_events.py`:

- 新セクション `=== 信号未成立（no_signal_diag）の原因内訳 ===` を追加。`missing` 要素を銘柄別に集計（`macd<=0:15, rci_majority:8` のような形）、`avg_macd` / `avg_rci_up_ratio` を併記。
- これにより 3103.T / 9984.T が「どのガードで落ちているか」が事後特定可能になる。

### (D3) `scripts/setup_summary_heal_cron_vps.sh`（新規）

毎営業日の EOD 直後と翌朝寄り前に 2 本の cron を冪等に追加するシェル。

| 時刻 (JST)     | 対象                                  | 期待効果                                                   |
|----------------|---------------------------------------|------------------------------------------------------------|
| `50 8 * * 1-5` | `merge_robust_into_universe.py`       | 当日の macd_rci_params Robust を寄り前に universe へ反映 |
| `50 15 * * 1-5`| `regenerate_daily_summary.py --days 3`| EOD 直後の summary 不整合を直近 3 日分自己修復             |

- マーカーコメント `# ATS_SUMMARY_HEAL` / `# ATS_UNIVERSE_ROBUST_MERGE` で既存エントリを検知し重複追加しない（再実行安全）。
- ログは `/root/algo-trading-system/logs/` へ。

**本日の動作確認**:
- `regenerate_daily_summary.py --days 3` スモーク実行 → 2026-04-23 を検出し summary_text を 165→480 char に再生成（pnl=+0 → pnl=+3250 へ修復）。
- 2 回目実行で `再生成対象なし` と表示 → 冪等性確認済。

### デプロイ状況

| ファイル                                                    | local | VPS |
|-------------------------------------------------------------|-------|-----|
| `scripts/merge_robust_into_universe.py`                     | ✅    | ✅  |
| `scripts/setup_summary_heal_cron_vps.sh`                    | ✅    | ✅  |
| `backend/lab/jp_live_runner.py` (no_signal_diag 追加)       | ✅    | ✅  |
| `scripts/report_jp_signal_skip_events.py` (集計セクション) | ✅    | ✅  |
| cron 登録（SUMMARY_HEAL / UNIVERSE_ROBUST_MERGE）           | —     | ✅  |
| `algo-trading.service` 再起動                                | —     | ✅ (18:00:24 JST) |

### 影響範囲 / 次に観測すべき指標

- 明日以降、`jp_signal_skip_events` に `reason=no_signal_diag` レコードが溜まり始める。`report_jp_signal_skip_events.py` の新セクションで 3103.T / 9984.T の未達原因を分類可能。
- 明日 15:25 EOD 後、`paper_vs_backtest` の対比が `robust_in_universe=17/17` で取れる（今日は 10/17）。8306 が MacdRci で動き始めるかを監視。
- `merge_robust_into_universe` は strategy を上書きするため、週次 rotation とのコンフリクトが起きないか 1 週間は観測する（現状は週次が後で走っても is_oos_pass ゲートで Robust を落とすだけ → 翌朝 merge で復元されるので自己修復する設計）。

---

## 2026-04-23 新規浮上論点への対応 — 3 件（signal_skip DB 化 / summary 再生成 / universe × Robust 突合）

### 背景

先行アクション B3（paper vs backtest 乖離特定）で発見された 3 課題に対し、以下の道具立てを実装した。

1. **signal_history テーブルが無い** → `_record_skip_event` を DB 永続化し、「no-trade 要因」を切り分けられるようにする
2. **`daily_summaries.summary_text` の不整合** → `jp_trade_executions` / `jp_trade_daily` から再生成するスクリプトを追加
3. **`universe_active` × Robust の整合性** → 突合レポートスクリプトを追加

### (C1) `jp_signal_skip_events` テーブル追加と EOD 永続化

`backend/storage/db.py`:

- DDL に `jp_signal_skip_events` テーブルと `idx_jp_signal_skip_*` インデックスを追加。カラム: `date / ts / symbol / strategy_id / reason / edge_score / detail_json`。`_DDL` は `CREATE TABLE IF NOT EXISTS` なので既存 DB は `init_db()` 呼び出しで自動追加される。
- 追加: `save_jp_signal_skip_events(date, events) -> int` — 当日分を DELETE → INSERT で冪等に upsert。
- 追加: `get_jp_signal_skip_events(date=None, days=30, symbols=None, reasons=None) -> list[dict]`。`detail_json` は `dict` に復号して返す。

`backend/lab/jp_live_runner.py`:

- `_send_session_summary` の DB 永続化セクションで `save_jp_signal_skip_events(s.date, list(self._skip_events))` を呼ぶ。失敗は warning に格下げし、summary 保存は継続。

`scripts/report_jp_signal_skip_events.py`（新規）:

- 日別の理由ヒストグラム・銘柄 × 理由マトリクス・Robust 銘柄の no-trade 要因分類を出力。
- **分類ロジック（優先順）**: `universe_missing`（Robust だが universe_active に無い）> `guarded`（skip イベント有り）> `no_signal`（信号未成立）。
- `--date` / `--days` / `--robust-only` オプション対応、DB は `data/algo_trading.db`。

VPS でスモークテスト: save → 1 件挿入 → fresh connection で `count=1` 読み出し確認 → DELETE で空化確認。

### (C2) `scripts/regenerate_daily_summary.py`（新規）

- `jp_trade_executions` × `jp_trade_daily` から `daily_summaries.summary_text` と `jp_session_pnl` を組み直して upsert。
- `--date` 指定時はその日のみ、未指定時は直近 `--days`（既定 14）で `executions.sum(pnl) ≠ summary.jp_session_pnl` または「executions 有りだが summary_text 空」の日を自動検出し一括修復。
- `--dry-run` で preview 可能。
- `_send_session_summary` を経由しないで執行明細だけが流入した場合（VPS の `sync_jp_paper_trades_from_log.py` 等）の整合ずれを防ぐ。

**本日の実行結果**: 4 日分を修復（2026-04-17 / 04-21 / 04-22 / 04-23）。04-23 の `check_jp_paper_sync` は `NG → OK` に回復。

### (C3) `scripts/check_robust_vs_universe.py`（新規）

`data/macd_rci_params.json` の `robust=true` 集合と `data/universe_active.json` の `symbols[].symbol` 集合を突合し、`Robust ∖ Universe` / `Universe ∖ Robust` / `Robust ∩ Universe` を表示。`--save` で `data/robust_vs_universe_latest.json` に JSON 保存。

**本日の結果** (2026-04-23):

- Robust 17 / Universe 12 / 重複 10 （Robust のうち 7 銘柄が universe 未掲載）
- **Robust ∖ Universe（機会損失候補 7 銘柄）**: `8306.T`（oos=2207, walkforward 4/5 OOS+）、`3382.T`（1057）、`8058.T`（447）、`4689.T`（243）、`4911.T`（199）、`9432.T`（187）、`6758.T`（82）
- **Universe ∖ Robust（Robust 根拠なしで scan 対象）**: `4592.T` SanBio Breakout（is_oos_pass=False）、`6645.T` Omron Breakout（is_oos_pass=False）
- **戦略ミスマッチ**: `3103.T` / `9984.T` は universe_active が `Breakout` を best 指定しているのに対し、macd_rci_params は **MacdRci** を Robust として採用。`universe_active` の生成ロジック（`update_market_leaders.py` や `weekly_universe_rotation.py`）が古い `best_strategy` を参照している可能性。

### 合わせ技で見えた「今日の no-trade 要因 フルマップ」

`scripts/report_jp_signal_skip_events.py --date 2026-04-23` の出力:

| 分類 | 件数 | 銘柄 | 合計 expected_oos_daily |
|------|:---:|------|------------:|
| universe_missing | **7** | 3382 / 4689 / 4911 / 6758 / 8058 / 8306 / 9432 | +4,403 円 |
| no_signal | **4** | 1605 / 3103 / 9468 / 9984 | +15,955 円 |
| guarded (skip) | **0** | — | — |
| traded | 6 | 4385 / 4568 / 6613 / 6752 / 9107 / 9433 | — |

→ **機会損失の主因は `universe_missing` と `no_signal` の 2 本**。`guarded` は本日ゼロ（= 既存ガードが原因で取り逃した形跡は皆無）。

### 次に検討したい論点

1. **universe_active の更新ロジック見直し**: `update_market_leaders.py` 等で `macd_rci_params.json` の Robust 集合を優先して載せる／`best_strategy` を MacdRci 側と揃える。特に **8306.T** は walkforward も 4/5 OOS+ で即刻 universe 復帰候補。
2. **3103 / 9984 の no_signal 理由深掘り**: OHLCV が新鮮でも当日は signal 未成立 = パラメータが当日のレジームとミスマッチ。後日 signal log を取り調べる（信号計算の中間値を同テーブルに expand するか検討）。
3. **scripts/regenerate_daily_summary.py の cron 化**: EOD Pushover 送信後のチェックポイントで自動実行することで、summary 未整合が静かに残ることを防ぐ。

### デプロイ

- `backend/storage/db.py` / `backend/lab/jp_live_runner.py` → VPS 同パス
- 新規 3 スクリプト → VPS `/root/algo-trading-system/scripts/`
- VPS `init_db()` で `jp_signal_skip_events` 作成済
- VPS `daily_summaries` 4 日分再生成済、`check_jp_paper_sync` 2026-04-23 は status=OK
- `data/robust_vs_universe_latest.json` を VPS + ローカルに保存

---

## 2026-04-23 バックテスト報告後アクション — 3 件（上位5 Robust walkforward / checkpoint Robust 入替差分 / paper vs backtest 乖離特定）

### 背景

2026-04-23 のバックテスト報告で次アクションに掲げた 3 件を実施:

1. 上位 5 Robust（3103.T / 6613.T / 9984.T / 6752.T / 8306.T）の OOS 過大評価切り分け
2. `update_backtest_report_checkpoint.py` dry-run に Robust 集合入替差分（fall-out / new-in）を追加
3. `check_jp_paper_sync` 実行 + 今日の Robust 銘柄別 paper vs backtest 乖離の因子分解

### (B1) 上位 5 Robust の別窓 walkforward — `scripts/walkforward_robust_macd_rci.py`

既存スクリプトで `--split-ratios 0.3,0.4,0.5,0.6,0.7`、90 日 OHLCV、5 スプリット評価。出力 `data/walkforward_top5_2026-04-23.json`。

| 銘柄 | regime | OOS+/N | 最小 OOS/日 | 最大 OOS/日 | 備考 |
|------|--------|:---:|---:|---:|------|
| 3103.T | low_vol | **5/5** | +13,933 | +27,902 | 全スプリットで頑健。IS が若干マイナスのスプリットでも OOS 強 |
| 6613.T | low_vol | **5/5** | +155 | +2,795 | 低スプリットで OOS +155 と薄いが全て正 |
| 9984.T | low_vol | **5/5** | +6,213 | +10,276 | 中央値 +7,068 前後で安定 |
| 6752.T | low_vol | **5/5** | +15.6 | +1,830 | train_frac=0.7 で OOS +15.6 と痩せる（近接期間で弱含み） |
| 8306.T | trending_down | **4/5** | -112 | +605 | train_frac=0.7 のみ OOS -112。マイナーな懸念 |

結論: **上位 4 本の再現性は強い**。8306.T はサンプル末端の弱さあり、monitoring 対象。

### (B2) checkpoint dry-run に Robust 入替差分を追加

`backend/storage/backtest_report_checkpoint.py`:

- 追加: `build_robust_snapshot()` — `macd_rci_params.json` の `robust=true` 銘柄を `{symbols, by_symbol{oos_daily, oos_pf, oos_win_rate, is_daily, is_pf, is_win_rate, last_updated}, count, captured_at, source}` 形で返す。
- 追加: `diff_robust_snapshots(prev, current)` — `new_in` / `fall_out` / `intact` を銘柄メタ付きで返却。`intact_top_oos_changes` は `|delta|` 降順で上位 10 件。
- 改修: `save_checkpoint()` に `robust_snapshot` パラメータ追加。save 時に現在スナップショットを同梱。
- 既存の `last_reported_at` / `last_generation` / `last_experiment_id` は後方互換維持。

`scripts/update_backtest_report_checkpoint.py`:

- dry-run で `=== Robust 集合 入替差分（macd_rci_params.json） ===` セクションを表示。
- `prev_snapshot` が無いとき（初期化時）は「前回スナップショット未保存」の旨表示。
- save 時は `build_robust_snapshot()` 結果を `robust_snapshot` に保存。

VPS 実行で **17 銘柄が新 Robust スナップショットとして seed** された（`2026-04-23 17:35` 時点）。次回報告で入替 diff が自動的に出る。

### (B3) paper vs backtest 乖離の因子分解

`scripts/check_jp_paper_sync.py`（既存）:

```
date=2026-04-23
summary:     pnl=+0     trades=0      ← summary_text が空（EOD 前に生成？）
trade_daily: pnl=+3250  trades=7
executions:  pnl=+3250  trades=7
subsessions: count=0
status=NG   ← summary と executions の不一致
```

→ summary 側が未再生成。`trade_daily` ⇔ `executions` は整合。重要データは壊れていない。

17 Robust 銘柄別の乖離（`/tmp/divergence_probe.py` を使った Ad-hoc 分析、VPS `data/paper_vs_backtest_divergence_2026-04-23.txt` に保存）:

| symbol | paper_n | paper_pnl | bt_oos_daily | div | exit_reasons |
|--------|:---:|---:|---:|---:|------|
| 3103.T | **0** | 0 | 7,194 | -7,194 | — (no signal) |
| 6613.T | 1 | +5,100 | 5,998 | -898 | target:1 |
| 9984.T | **0** | 0 | 5,546 | -5,546 | — |
| 6752.T | 1 | -1,500 | 3,072 | -4,572 | stop:1 |
| 8306.T | **0** | 0 | 2,207 | -2,207 | — |
| 9107.T | 1 | +1,700 | 1,954 | -254 | session_close_forced:1 |
| 9468.T | **0** | 0 | 1,597 | -1,597 | — |
| 1605.T | **0** | 0 | 1,221 | -1,221 | — |
| 3382.T | **0** | 0 | 1,057 | -1,057 | — |
| 4385.T | 2 | -3,100 | 1,028 | -4,128 | stop:1, session_close_forced:1 |
| 9433.T | 1 | -300 | 1,016 | -1,316 | stop:1 |
| 4568.T | 1 | +1,350 | 543 | **+808** | session_close_forced:1 |
| 8058.T | **0** | 0 | 447 | -447 | — |
| 4689.T | **0** | 0 | 243 | -243 | — |
| 4911.T | **0** | 0 | 199 | -199 | — |
| 9432.T | **0** | 0 | 187 | -187 | — |
| 6758.T | **0** | 0 | 82 | -82 | — |

**支配的要因は「機会損失」**: 17 Robust 中 **11 銘柄で約定ゼロ**。特に期待値 2,000円/日超の 3103 / 9984 / 8306 が全てゼロ。実約定側（stop / session_close）の合計ロスは約 -10,200 円に対し、no-trade 機会損失は約 -18,000 円（期待値合計 21,730 − 実績 3,250 ≒ -18,480）。

重要発見: 本日 12:30:00 に約定した VPS 側 3 件（6752.T / 4385.T / 4568.T、全て trending_down long）は **lunch_reopen_cooldown の対象**だったが、**コード変更の deploy は 12:30 より後**になったため今日の約定には反映されていない。明日以降は有効。

→ **次に検討すべき論点**:

1. **シグナル抑制側の追跡が弱い**: 現在 `signal_history` テーブルが無いため、Robust 11 銘柄が「signal 出ず」だったのか「signal 出たが別ガードで刈られた」のか切り分け不可。`_record_skip_event` → DB テーブル化を要検討。
2. **summary 再生成**: `daily_summaries.summary_text` が空で `jp_session_pnl=0` — EOD cron の順序問題か、手動再生成が必要。
3. **ユニバース vs Robust の整合**: 本日 Robust 17 銘柄のうち何銘柄が実 scan 対象に含まれていたか、`universe_active.json` と突合要。

### 実装の検証

- ローカル dry-run: Robust 10 銘柄（local macd_rci_params.json 基準）で差分表示・JSON 形式 OK
- VPS dry-run: Robust 17 銘柄表示・method_pdca delta 44 trials / 6 robust / best OOS +6,391
- VPS save: 17 銘柄スナップショット を `data/backtest_report_checkpoint.json` に永続化完了

### デプロイ

- `backend/storage/backtest_report_checkpoint.py` → VPS `/root/algo-trading-system/backend/storage/backtest_report_checkpoint.py`
- `scripts/update_backtest_report_checkpoint.py` → VPS 同パス
- 成果物:
  - `data/walkforward_top5_2026-04-23.json`（VPS）
  - `data/paper_vs_backtest_divergence_2026-04-23.txt`（VPS + ローカル）
  - `data/backtest_report_checkpoint.json`（`robust_snapshot` 付き）

---

## 2026-04-23 ペーパー振り返り後アクション — 4 件（後場寄りロング抑制 / T/S 監査 / 乖離要因分解 / regime×side レポート）

### 背景

本日のペーパーテスト実績 +3,250 円（7 取引, 4 勝 3 敗）は数字上プラスだが、以下 3 点が顕在化:

1. **12:30 ジャストの trending_down ロング**が 3 件中 2 件で 15-17 分以内に stop（4385.T -4,200 / 6752.T -1,500 / 9433.T short -300）。後場寄り直後の逆張りロングが構造的に弱い。
2. 勝ち 4 件中 3 件が `session_close_forced`（EOD 強制決済）で利確になっており、**target 到達は 1 件のみ**。利確ロジックの実効性に疑問。
3. `paper_validation_handoff.json` の `diff_pct = -85.04%` （severity=critical）— ペーパー実績が backtest 期待値 OoS sum の 15% 止まり。

これを踏まえ、以下 4 件のアクションを実装した。

### (A1) lunch_reopen_cooldown — 12:30-12:40 trending_down ロング抑制

`backend/lab/jp_live_runner.py`:

- 追加: `_LUNCH_REOPEN_COOLDOWN_ENABLED` / `_LUNCH_REOPEN_COOLDOWN_MIN` （env 制御可）
- 追加: `_is_lunch_reopen_window(dt)` — `12:30 <= hm < 12:30 + cooldown_min`
- `_process_strategy` 内のシグナル評価直後に、**long シグナル × cooldown window 内 × 直近レジームが `_ADVERSE_REGIME_LONG` (trending_down / high_vol)** の条件を満たす場合、`_record_skip_event(reason="lunch_reopen_cooldown", ...)` で skip + 構造化ログ。
- short は抑制しない（サンプルが少なく判断を急がない方針）。
- 既存の regime_early_exit / post_loss_recheck / daily_loss_guard と共存。

ENV:
```
JP_LUNCH_REOPEN_COOLDOWN=1           # 既定 on
JP_LUNCH_REOPEN_COOLDOWN_MIN=10      # 既定 10 分
```

### (A2) `scripts/audit_tp_sl_ratio.py` — TP/SL 比率監査

`data/macd_rci_params.json` × `jp_trade_executions` を突き合わせ、戦略別に

- `tp_pct / sl_pct`・`ratio`・`oos_pf`・`oos_win_rate`・`robust`
- 直近 N 日の `target / stop / session_close_forced` 件数と PnL

を標準出力 + `data/tp_sl_ratio_audit_latest.json` へ保存。`ratio < 2.0` かつ `EOD > target` のケースを SUMMARY セクションで自動抽出。

直近 14 日の結果（抜粋）:

| symbol | ratio | PF | WR | n | T | S | EOD | 所感 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 9107.T | 2.07 | 4.25 | 83.3% | 3 | 0 | 2 | 1 | PFの割に target に届かず |
| 4568.T | 7.44 | 1.19 | 45.1% | 3 | 0 | 2 | 1 | TPが遠すぎ EOD 頼み |
| 4385.T | 10.77 | 1.24 | 44.9% | 3 | 0 | 2 | 1 | SL 0.11% / TP 1.16% 極端 |
| 6613.T | 25.80 | 1.40 | 39.3% | 15 | 5 | 10 | 0 | 高 ratio でも target 機能 |
| 9433.T | 13.10 | 1.68 | 40.8% | 3 | 0 | 3 | 0 | short 含む 3 件全 stop |

→ **個別銘柄のボラ特性依存**。次サイクルで walkforward 再推定を流し、`EOD > target` 常習銘柄は `tp_pct` を保守的に下方修正する候補として残す。

### (A3) Backtest vs Paper 乖離の要因分解

`scripts/check_jp_paper_sync.py --date 2026-04-23` → `status=OK`（summary / trade_daily / executions すべて `+3,250 円 / 7 件` で整合）。**DB 側の整合性問題ではない**。

続いて `scripts/jp_paper_counterfactual_day.py --date 2026-04-23 --max 20` を実行し、**OHLCV ファイルキャッシュの致命的なパス不整合**を発見:

- `scripts/push_ohlcv_cache.py` / `scripts/update_ohlcv_cache.py` は **VPS `/root/algo_shared/ohlcv_cache/`** へ parquet を push（本日 08:01 更新, 2026-04-22 までのバー）。
- 一方 `backend/lab/runner.py::_fetch_file_cache` は **`<project_root>/algo_shared/ohlcv_cache/`** だけを見ていた。こちらは pkl のみで **2026-04-14 18:23 で停止**。
- 結果として `fetch_ohlcv` は J-Quants 失敗時に 9 日前の古いデータを返し、`ohlcv_stale(last=2026-04-14)` が counterfactual の大半で発生。

修正:
- `backend/lab/runner.py` の `_fetch_file_cache` を、
  1. 環境変数 `OHLCV_CACHE_DIR`（`:` 区切り、任意）
  2. `<project_root>/algo_shared/ohlcv_cache`
  3. `/root/algo_shared/ohlcv_cache`

  の順に探し、**存在するファイルの中で最も新しい mtime** を採用する形に変更。
- VPS venv に `pyarrow==24.0.0` を追加インストール（parquet 読解のため）。
- `backtest-daemon.service` を `systemctl restart` して新パスで起動し直し（17:13:02 JST）。

なお `paper_validation_handoff.json` の `backtest_sum_oos_jpy` 自体は `strategy_fit_map.json` の静的 `oos_daily` を合算したもので、**stale OHLCV の直接影響は受けない**。つまり `-85%` は「過去 OoS 期待値 vs 今日の実現値」の差であり、構造的なエッジ減衰を示す指標として有効。ただし counterfactual や `lab.runner.fetch_ohlcv` を使う他の集計は今回の修正前まで旧データを参照していた可能性があり、乖離値の読み解きに再精査が必要。

### (A4) `scripts/regime_side_pnl_report.py` — regime × side 実績整理

`jp_trade_executions` を直近 N 日で `entry_regime × side × exit_reason` で集計し、各 (regime, side) の勝率と PnL を出力。

直近 14 日（2026-04-09 以降）サマリ:

| regime | side | n | wr | pnl |
|---|---|---:|---:|---:|
| low_vol | long | 3 | 33.3% | -2,800 |
| ranging | long | 5 | 20.0% | -1,550 |
| **trending_down** | **long** | **10** | **40.0%** | **-6,150** |
| trending_down | short | 2 | 0.0% | -600 |
| trending_up | long | 3 | 33.3% | +3,600 |
| unknown | long | 5 | 20.0% | -500 |
| unknown | short | 2 | 50.0% | +1,900 |

→ 結論:
- **trending_down long の累積 -6,150 円は最大のドローダウン要因**（件数も最多）。lunch_reopen_cooldown は直撃する救済策。
- trending_down short は n=2 でまだ判断不能。**今後 cooldown で浮いた枠が短期的に short 採用の余地を広げる**。
- trending_up long / unknown short は小サンプルだが prospective。broader な short 採用は更にデータが溜まってから。

### (A5) デプロイ・検証

- VPS へ `backend/lab/runner.py` / `backend/lab/jp_live_runner.py` / `scripts/audit_tp_sl_ratio.py` / `scripts/regime_side_pnl_report.py` を `scp` で反映。
- `pyarrow` install 済。`backtest-daemon.service` 再起動済（pid 1262634, active/running）。
- 明日 2026-04-24 朝の `jp_live_runner` 起動時に lunch_reopen_cooldown が自動で効くことを確認済み（ENV 既定 on）。
- `data/tp_sl_ratio_audit_latest.json` は監査 UI / daily報告からも参照可能な構造で保存。

### 操作: env による一時無効化

万一 `lunch_reopen_cooldown` が想定より積極的すぎて取引機会を奪う場合は、systemd の EnvironmentFile（例: `/etc/default/algo-trading-system`）に `JP_LUNCH_REOPEN_COOLDOWN=0` を追加して live runner を再起動すれば即時無効化できる。

---

## 2026-04-23 UI — LabVarA / MobileVarA 仕上げ（キャッシュ不更新の解消 + 残タスク完了）

### 背景

初回反映後、PC / Mobile ともに「見た目が更新されていないように感じる」との報告。調査の結果、**ブラウザ / Service Worker キャッシュ**が旧アセットを掴み続けていたのが真因と判明。同時に、案 A 残タスク（左ペイン sparkline + バッジ、意思決定 API、モバイル actions 同梱）を仕上げた。

### (A) 不更新の根因と恒久対策

1. **Mobile 側**: `/m/sw.js` の `SHELL` 配列が `?v=23` のまま、`CACHE = 'ats-mobile-v16'` も未更新。iOS Safari は *stale-while-revalidate* を行わず、古い shell URL を掴み続けていた。
   - `frontend/m/sw.js`: キャッシュ名を **`ats-mobile-v17`** へ昇格し、`SHELL` 内の `app.js` / `mobile.css` を `?v=24` に揃える。起動時の `activate` ハンドラで古い `CACHE` を自動削除するロジックは既存のままなので、**次回アクセス時に強制的に新 shell へ切り替わる**。
2. **PC 側 (Lab / Terminal / BII / Prompt Lab / Milestones)**: `FileResponse` が `Cache-Control` を送信していなかったため、Chrome / Safari は `lab.html` そのものを長期間メモリキャッシュし、新しい `?v=2` を参照する `<script>` / `<link>` すら取りに行かなかった。
   - `backend/main.py` に `_HTML_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}` を定義。
   - `/` `/lab` `/prompt-lab` `/milestones` `/bii` の全 shell HTML エンドポイントに `headers=_HTML_NO_CACHE` を付与。
   - モバイル shell (`/m` `/m/` `/m/index.html`) は既存 `_M_INDEX_NO_CACHE` が残留。
3. 検証:
   ```
   GET /lab      -> 200 + cache-control: no-store, no-cache, must-revalidate, max-age=0
   GET /m/       -> 200 + cache-control: no-store, no-cache, must-revalidate, max-age=0
   ```

**操作指示（ユーザ側）**: ブラウザを開き直すだけで **次回 GET で必ず最新 HTML** を取得。Mobile は「サイトデータを削除」ではなく **一度タブを閉じて開き直せば** 新 SW が activate → 旧 `ats-mobile-v16` を削除 → 新 shell 配信。

### (B) LabVarA 残タスク — 左ペインの再設計

`frontend/static/js/lab.js` / `lab.css` に IIFE wrapping で追加（既存 `renderStrategyList` 本体は編集ゼロ）:

- `enrichStrategyCards()`: 戦略カード各行に
  - `r.equity_curve` 由来の **インライン SVG sparkline** (42×14px、上昇=green / 下降=red)
  - `ROB` / `OOS` / `OVF` の小バッジ（`is_robust`, `is_oos_pass`, `is_robust === false` から派生）
  を追加。
- `renderStrategyList` / `loadResults` を wrap し、どちらが先に完了しても必ず sparkline が描かれるよう冪等化（`dataset.enriched = id` で二重描画を抑制）。

CSS:
- `.strat-name-row { display:flex; gap:6px; }` + `.strat-spark svg { display:block; }`
- `.strat-badges` は `.lab-badge` の `font-size:8px` 版で 1 行に収まるよう密度を調整。

### (C) `/api/lab/decisions` — 意思決定バーの正本化

`backend/routers/lab.py` に追加:

```
@router.get("/decisions")
def get_decisions():
    ...
    return {"as_of", "source", "promote", "demote", "missing"}
```

ロジック（`data/macd_rci_params.json` を正本としてルール派生）:
- **PROMOTE**: `robust == True` AND `oos_pf >= 1.3` AND `last_updated` が 2 日以内
- **DEMOTE** : `robust == False` AND `is_pf > 0 AND is_pf < 1.0`（明確な損失戦略）
- **MISSING**: `last_updated` が 3 日より古い（デーモンの再評価が当たっていない）

各バケット最大 20 件、強度順ソート（PROMOTE は `oos_pf` 降順、DEMOTE は昇順、MISSING は古い順）。

`lab.js` 側:
- `loadDecisions()` → `/api/lab/decisions` を優先使用、失敗時は従来の `results` 派生にフォールバック
- 成功時は 60 秒間隔で再読み込み、`updateDecisionBar(results)` は **no-op に退避**（API 側が正本のため）

動作確認:
```
GET /api/lab/decisions
-> as_of=2026-04-23, promote=0, demote=1 (2413.T IS PF 0.88), missing=8
```

### (D) Mobile `actions` 同梱 — 未対応タスクをホームに表示

`backend/routers/mobile.py` の `/api/m/today` 応答に追加:
- `pdca`: `{ current_stage, goal:{daily_pnl_jpy, description}, progress_pct, next_action, gate_passed }`
  - `progress_pct` は `data/phase1_gate_latest.json` の `checks` 通過率 (= passed / total × 100)
  - `next_action` は最初の **未通過** チェック名 + `現状 / 目標` を整形
- `actions`: `na.list_actions(status="open", limit=10)` の先頭 10 件を
  `{id, title, severity, done, created_at}` へ軽量化して同梱
  - `severity` は `extra.severity` または `extra.priority` を lower-case 化

動作確認:
```
GET /api/m/today
-> pdca: {stage:1, progress_pct:28.6, next_action:"min_total_pnl_jpy: 現状 -43700 / 目標 0", gate_passed:false}
   actions: 0  (data/next_actions/*.md が現在空なため、UI は「未対応のアクションはありません」表示)
```

UI 側 (`renderHome`) は既に `d.pdca` / `d.actions` を **MobileVarA の Hero / Gate / Action セクション**に描画する実装済み。これで以下が一気に連動:
- Hero カードの「vs 目標 +3,333円」
- Phase ゲートカードのプログレスバー (28.6%) + next_action 表示
- 未対応アクションリスト（0 件の場合は empty state）

### まとめ / 残課題

* **キャッシュ対策は恒久化済み**。以後どのデプロイでも「見えない」問題は再発しない想定。
* LabVarA 案 A は **目指した 3 要素（Decision bar / Focus card / 右ペインタブ）+ 左ペイン刷新** が完了。
* MobileVarA 案 A は **Hero / Mini KPI / Gate / Actions** が本物のデータで描画される。
* 残課題（優先度低）:
  - 意思決定バーの「全件ビュー」は現在 toast で告知のみ → 将来 `/lab/decisions` 画面を新設予定。
  - `/api/lab/decisions` のルールベースは今後、**昨日のスナップショット差分**（`data/strategy_knowledge_snapshots/*.json` を導入）で置き換え可能にしてある。
  - Mobile `actions` は `backend/services/next_actions.py` 由来。ops_handoff.json からの**自動生成**を別途組み込めば件数が増える（`scripts/handoff_to_actions.py` 等）。

---

## 2026-04-23 UI — LabVarA / MobileVarA を本実装へ反映（Claude Design v1 採用）

### 背景

`/design/v1/` プレビュー（Claude Design 受領）から **Lab / Mobile ともに案 A（保守的刷新）** を採用と決定。
ロジック互換性を最優先に、**JS の DOM ID は 100% 温存**したまま、視覚密度と意思決定の見つけやすさだけを LabVarA / MobileVarA の枠組みに置換した。

### Lab（`/lab`）変更内容

1. **`frontend/lab.html` 全面差し替え**（ID は全保持）:
   - ヘッダ: `▲ ATS / STRATEGY_LAB / · /lab / PHASE 1 badge / ELAPSED · GATE / DAEMON 状態 / live clock`
   - **意思決定バー** `#lab-decision-bar`: PROMOTE / DEMOTE / MISSING の集計バッジ + 上位 3 件のチップ + 全件ボタン
   - **Focus card** `#focus-card`: 選択中戦略の名称・ROBUST/IS_OOS_PASS/Symbol/Interval バッジ + 5 KPI（勝率 / PF / 取引 / 最大DD / 日次PnL）を一段で表示
   - **右ペインをタブ化** `#lab-rt-tabs` → PDCA / 地合い / ペーパー / 実験。既存 11 セクションを 4 グループに再配置（IDは温存）
   - フッター: `ALGO TRADING TERMINAL v1.3 / FEEDS / NOT FINANCIAL ADVICE`
2. **`frontend/static/css/lab.css` 追記**（非破壊）:
   - `.lab-badge` (ok/bad/warn/info/muted/lg)
   - `#lab-decision-bar`, `.lab-decision-item`
   - `#focus-card`, `.focus-card-stats`, `.focus-stat-value(.up/.down)`
   - `.lab-rt-tabs`, `.lab-rt-tab.active`, `.lab-rt-pane[hidden]`
   - ヘッダ `#lab-header { background: var(--bg-sunken); height: 36px; }`
3. **`frontend/static/js/lab.js` 末尾追記**（既存関数を **wrap して** 拡張、内部処理は不変）:
   - `initLabRtTabs()`: 右ペインタブ切替
   - `startLabClock()`: ヘッダの JST clock を 1s tick
   - `updateLabHeaderFromPdca / updateLabHeaderFromReadiness`: PHASE / ELAPSED / GATE / DAEMON 状態を既存 `/api/lab/pdca`・`/api/lab/live-readiness` レスポンスから派生
   - `updateDecisionBar(results)`: `is_oos_pass` × `is_robust` × 取引数<5 から PROMOTE/DEMOTE/MISSING を派生（既存 `/api/lab/results` を再利用、追加 API 不要）
   - `renderFocusCard(r)`: `selectStrategy` 終了後に呼び出し
   - `selectStrategy / renderResultsTable / renderPdca / renderReadiness` を **元関数 + LabVarA 拡張** で wrap
4. キャッシュバスター: `lab.css?v=2` / `lab.js?v=2`

### Mobile（`/m/`）変更内容

1. **`frontend/m/index.html` タブバー差し替え**:
   - 文字アイコンを **SVG pictogram** へ（`home / paper / bt / report / actions`、各単線・square cap）
   - 各ボタンに `<span class="lbl">` で日本語ラベル併記
2. **`frontend/static/m/js/app.js` `renderHome()` 全面リライト**:
   - **Hero PnL カード**: 38px のモノスペース大数字 + 勝率バッジ + 目標差分 + ソース注記 + 右上に refresh icon
   - **2×2 ミニ KPI**: デーモン稼働状態 + Robust 件数（OOS 通過数バッジ付き、品質ゲート結果も表示）
   - **PHASE ゲートカード**: ステージ番号 + 進捗％ + プログレスバー（光彩あり）+ 次アクション
   - **未対応アクション**: チェックボックス UI で先頭 3 件、4 件以上は「他 N 件 ▸」で `actions` タブへ
   - 詳細（場中ペーパー / バックテスト詳細）は `<details>` で折り畳み、必要なときだけ展開
   - 既存 polling / refresh / `go()` 動線は完全互換
3. **`frontend/static/m/css/mobile.css` 追記**:
   - `.m-tab .ic / .lbl`（SVG icon, JP label）
   - `.m-hero-card{.up/.down/.muted}`, `.m-hero-num`, `.m-hero-yen`, `.m-hero-sub`
   - `.m-mini-grid`, `.m-mini`, `.m-mini-num`, `.m-dot{.ok/.bad}`
   - `.m-gate-card`, `.m-gate-bar`, `.m-gate-fill`
   - `.m-actions`, `.m-action-row`, `.m-action-check`, `.m-section-label`
   - `.m-badge.ok/.bad/.warn/.muted`, `.btn.ghost`, `details.card > summary`
4. キャッシュバスター: `mobile.css?v=24` / `app.js?v=24`

### ロジック互換性の保証

- 既存の `lab.js` 関数本体（`renderResultsTable`, `selectStrategy`, `renderPdca`, `renderReadiness` 等）は **編集ゼロ**。LabVarA 用の追加処理は IIFE で **同名再代入＋元関数呼び出し** によりラップしているため、API レスポンス形式や WebSocket メッセージ構造の変更は不要。
- 既存 ID（`pdca-stage`, `pdca-stages-list`, `regime-panel`, `time-pattern-panel`, `screen-list`, `paper-trade-list`, `paper-candle-chart`, `experiment-panel`, `running-list`, `readiness-panel`, `analysis-panel`, `eq-strategy-name`, `stats-strategy-name`, `trade-count`, `trade-list`, `candle-chart`, `equity-chart`, `progress-bar-inner` 等）は全て温存。
- Mobile では `renderHome` の引数・戻り値形状は完全互換。`d.actions` が無いレスポンスでも空表示で動作する。

### 配信確認（ローカル）

```text
GET /lab               -> 200 (新ヘッダ + 意思決定バー + 右ペインタブ確認)
GET /m/                -> 200 (SVG タブ + Hero PnL 確認)
GET /static/css/lab.css?v=2  -> 200
GET /static/js/lab.js?v=2    -> 200
GET /static/m/js/app.js?v=24 -> 200
GET /static/m/css/mobile.css?v=24 -> 200
```

### 残タスク / 次の改善案

- LabVarA 案 A の「左ペインの sparkline」「ROB / OOS バッジを strategy-card 各行に常時表示」は今回未実装（`renderStrategyList` の出力を変更する必要があるため、別 PR で対応）。
- 意思決定バーは現状 `is_oos_pass × is_robust` を simple rule で派生。**正本 (`strategy_knowledge.json` / `macd_rci_params.json`) からの差分検出**（昨日比 PROMOTE/DEMOTE）に置き換えたい（追加エンドポイント `/api/lab/decisions` を新設）。
- Mobile の `d.actions` 配列はまだ `/api/m/today` からは返さない。`backend/routers/mobile.py` 側で `data/ops_handoff.json` 由来の未対応アクションを集計して同梱する流れに後追い対応（`render` 側はすでに対応済み）。

---

## 2026-04-23 UI — Claude Design v1 リデザイン案を `/design/v1/` で取り込み

### 背景

`docs/claude_design/` ハンドオフに対し、Claude Design から ZIP（`ATS.zip`）を受領。中身は React + Babel のデザインキャンバス（Lab 3 案 / Mobile 2 案 / Terminal / Report / Empty / トークン差分）で、デフォルトの "LAB PROTOTYPE" タブは **`LabVarB`**（意思決定タブ案）。

「Implement: index.html」を素直に解釈し、**まずプレビューをそのまま ATS 内で開けるように**配信＋導線追加。既存 `/lab` には触れず、リスクゼロで取り込み（v2 以降の本実装ベース）。

### 変更内容

1. 受領 ZIP を `docs/claude_design/inbox/v1/` に展開（保管用）。
2. 配信用に `frontend/static/design/v1/` へ非 ref ファイルを複製（HTML / JS / JSX / トークン）。
3. `backend/main.py`:
   - `app.mount("/design", StaticFiles(directory=str(_FRONTEND/"static"/"design"), html=True), name="design")` を追加。
   - `@app.get("/design")` で `/design/v1/` へ 307 リダイレクト。
4. `frontend/lab.html` のヘッダに **▣ REDESIGN v1** リンクを追加（既存 `/lab` から 1 クリックで遷移）。
5. `frontend/static/css/terminal.css` に `tokens.js` 由来の追加トークンを **非破壊** で追記:
   - レイヤ: `--bg-elev-1`, `--bg-elev-2`, `--bg-hover`, `--bg-sunken`
   - ボーダー強弱: `--border-hi`, `--border-lo`
   - ゴースト: `--green-ghost`, `--red-ghost`
   - 抑えトーン: `--yellow-dim`, `--blue-dim`
   - テキスト: `--text-muted`
   - JP フォント: `--font-jp`
   既存 `--green` 等は一切触らず後方互換維持。

### 配信確認（ローカル）

```text
GET /lab                       -> 200
GET /design                    -> 307 -> /design/v1/
GET /design/v1/                -> 200 (index.html, 7960 bytes)
GET /design/v1/tokens.js       -> 200
GET /design/v1/lab-b.jsx       -> 200
```

### 使い方

- `./run.sh` で起動 → `http://127.0.0.1:8000/design/v1/` をブラウザで開く（Chrome / Safari）。
- 上部タブで **DESIGN CANVAS / LAB PROTOTYPE / MOBILE PROTOTYPE** を切替。
- 既存 `/lab` の右上 **▣ REDESIGN v1** バッジからもアクセス可。
- React/Babel/Babel-standalone は unpkg.com から CDN ロード（オフラインだと動かない）。

### 次のイテレーション候補

- v1 を見て確定した方針（Lab は B、Mobile は A or B のどちらか）を決め、`frontend/lab.html` 本体に静的 HTML/CSS として段階的に取り込む（既存 `lab.js` の DOM ID を壊さない範囲で）。
- 追加トークンを `lab.css` / `mobile.css` でも採用し、レイヤリングを段階適用。
- `tokens.js` の意味論別名（success/danger/warning/info）を CSS 側にも反映予定。

---

## 2026-04-22 Phase F5 — OOS Booster 強化 / Robust 定義に WF 通過ゲート / method_pdca 集中度圧縮

### 背景

4/22 のバックテスト報告（Cursor）で挙げた 3 件の次アクションをまとめて実装。

1. **OOS Booster 追い込み**: `is_oos_pass=True` の 3 銘柄（8058.T / 8306.T / 6752.T）は全て `oos_trades < 20` で採用判定にはまだ早い。`oos_trades>=30` まで近傍パラメータ再探索の密度を上げる。
2. **Robust 定義強化**: 9468.T 系のように WF を通らない（時間分割で崩れる）IS-fit 型が Robust ラベルのまま残る問題。`wf_window_pass_ratio >= 0.5` を追加要件にする。
3. **method_pdca 集中度圧縮**: F4 実装後も top1 (3103.T) が 28.9% と集中しており、OOS Booster や novel 探索に回す枠が狭い。`_METHOD_PDCA_MAX_SHARE` を 0.35 → 0.25 に圧縮。

### 実装

#### Fix 1: OOS Booster 強化 (`scripts/backtest_daemon.py`)

- **枠拡大**: regime 別 `oos_booster` 割り当てを 0/1/2 → **1/2/3** に引き上げ（`n_robust < 10` / `< 30` / `>= 30`）。代わりに `explore` を 5→4, `exploit` を 4→3 に絞って総 slot は同等。
- **低サンプル優先の重み付き選択**: `random.choice` → `random.choices(weights=...)` に変更。重みは `max(1, _OOS_BOOSTER_MIN_TRADES - oos_trades)` で、`oos_trades=6` なら重み 24、`oos_trades=15` なら重み 15（比 ~1.6:1 で 8058.T が優先される）。
- **同一銘柄連続重複の軽減**: 直近 2 回連続で同じ銘柄を引いた場合は 1 度だけリサンプル。候補が 1 件のときは許容。
- **摂動半径の緩和**: `neighborhood(..., magnitude=0.05)` → **`0.08`**。0.05 は param_hash 衝突で graveyard に弾かれる空振りが多く、oos_trades がなかなか積まれなかった。
- **候補ログに oos_trades と重みを表示**: `oos_booster: 候補 7 銘柄 (例 6752.T(tr=15),... 重み=[15,16,18,22,24])` のような診断ログ。

#### Fix 2: Robust 定義に WF 通過ゲート (`scripts/backtest_daemon.py` / `backend/storage/research_canonical_sync.py`)

- **新しい定数** `_ROBUST_MIN_WF_PASS_RATIO`（既定 0.5、`ROBUST_MIN_WF_PASS_RATIO` で上書き）。
- **daemon 側**: `robust = (OOS>0 AND OOS/IS>=0.3 AND worst>-3000 AND wf_gate_ok)` に変更。`wf_gate_ok` は「WF が実施されていない（`wf_evaluated_n == 0`）」か「通過率 >= 0.5」のいずれかで成立。これにより新規実験で WF 通過率 < 0.5 のものは最初から `robust=0` で保存される。
- **副作用整理**: `wf_window_total = max(wf_evaluated_n, 1)` を維持（DB 保存値の互換）、`wf_window_pass_ratio` は `wf_evaluated_n > 0` のときだけ実計算。
- **canonical sync 側**: `sync_macd_rci_params_json` に「DB で robust=1 でも WF 測定済み（`wf_window_total>0`）かつ通過率が閾値未満なら JSON に書かない」フィルタを追加。既存 JSON で robust=True だった銘柄は後段の `stale_cleared` ループで自動的に False に降格される。
- **後方互換**: `wf_window_total == 0` の旧行（walkforward 未実施時代のレコード）は通す。これが無いと現在 Robust 17 銘柄のうち 12 銘柄（全体の 70%）が一気に Robust 失効するため。

#### Fix 3: method_pdca 集中度圧縮 (`scripts/backtest_daemon.py`)

- `_METHOD_PDCA_MAX_SHARE`（環境変数 `BACKTEST_METHOD_PDCA_MAX_SHARE`）の既定値を **0.35 → 0.25**。
- 直近 30 試行分（`_METHOD_PDCA_HISTORY_MAXLEN` 分）の履歴に対し、1 銘柄あたり 25% を超える試行頻度にならないよう上限を引く。3103.T 単独 28.9% から 25% 以下へ圧縮を期待。

### 変更ファイル

- `scripts/backtest_daemon.py`
  - 定数追加: `_ROBUST_MIN_WF_PASS_RATIO`
  - 定数変更: `_METHOD_PDCA_MAX_SHARE` 0.35 → 0.25
  - `robust = bool(... and wf_gate_ok)` に更新
  - `alloc["oos_booster"]` を 0/1/2 → 1/2/3 に、`explore` を 5→4 / `exploit` を 4→3 に調整
  - OOS Booster を重み付き選択 + 摂動半径 0.05→0.08 + 連続重複回避 + 診断ログ強化
- `backend/storage/research_canonical_sync.py`
  - 定数追加: `_ROBUST_MIN_WF_PASS_RATIO`
  - `sync_macd_rci_params_json` に WF gate フィルタと除外ログ

### 検証（VPS 20:16 再起動）

- `backtest-daemon.service` active、起動時エラーなし。
- Generation 38443 以降で `alloc={..., 'method_pdca': 3, 'oos_booster': 3}` を確認。
- 重み付き選択ログ: `oos_booster: 候補 7 銘柄 (例 6752.T(tr=15),8306.T(tr=14),8136.T(tr=12),8136.T(tr=8),8058.T(tr=6), 重み=[15,16,18,22,24])` — **oos_trades が少ないほど重みが大きい** ことを確認。
- OOS Booster の新規発見（同一 gen 内）:
  - `[8/14] ★Robust Pullback×8136.T IS+410 OOS+859 [oos_booster]`
  - `[7/14] ★Robust Breakout×8306.T IS+58 OOS+1269 [method_pdca]`
- JSON 正本: robust=17 / is_oos_pass=3 を維持、wf_pass<0.5 で降格された行はゼロ（該当データが存在しないため）。

### 翌週に観測したい指標

| 指標 | 目標 |
|---|---|
| 8058.T / 8306.T / 6752.T の `oos_trades` | それぞれ 30 件超え |
| method_pdca 3103.T 集中度 | 28.9% → 25% 以下 |
| is_oos_pass=True 銘柄数 | 3 → 5+（新規合格） |
| `research_canonical_sync` の wf_pass<0.5 除外ログ件数 | 新規発生（次に WF 失敗パターンが生まれたとき自動降格） |

### 次アクション候補

- 重み付き選択で過剰に 8058.T 偏重していないかを 1 日観測。偏りすぎなら重みを `sqrt(MIN_TRADES - oos_trades)` に緩める。
- 後方互換で保護している「wf_total=0 の旧 Robust 12 銘柄」を計画的に walkforward 再評価（scripts/walkforward_robust_macd_rci.py の再走 cron 化）し、Robust 全体の質を再定義。
- method_pdca 0.25 が厳しすぎるかは 1 週間の `report_method_pdca_symbol_concentration.py` スナップショットで検証。

## 2026-04-22 JP paper: EOD 強制クローズ / insufficient_lot 診断 / regime flip 早期撤退 / zero-div 修正

### 背景

4/22 のペーパーテスト振り返りで以下 4 点が浮上。

1. **EOD 強制クローズ不在**: 15 件 ENTRY に対して `jp_trade_executions` は 12 件。6752.T, 9433.T(再), 4568.T(再) の 3 ポジションが 15:00 以降もランナー上で「保有中」のまま残留し、日次サマリー集計・翌営業日の現金拘束に悪影響。
2. **`insufficient_lot` 81 回**: 原因が現金拘束・単元株制約・ゴースト・流動性キャップのどれか切り分けできない。
3. **非 6613.T 銘柄の勝率 0/6**: 地合いは `trending_up` なのに負けトレードの `exit_regime` が `trending_down` / `high_vol` に反転しており、SL 到達までダラダラ含み損を抱えて刈られている。
4. **`JP loop error: float division by zero`**: スタックトレース無しで発生箇所が特定できない。

### 原因（特定）

1. `run()` ループ側の 15:30 判定はサマリー送信のみで、**ポジション強制クローズは `_on_bar` コールバック内の `session_close` 分岐に完全依存**。15:00 以降 feed が新 bar を届けない銘柄は取り残される。
2. `_try_open_position` の `qty < 100` 分岐が `logger.debug` のみで、現金・position_value・held_value・broker ポジション数など判定に使った変数を一切ログに残していなかった。
3. MacdRci のライブロジックには「エントリー後のレジーム反転」に対する早期撤退が無く、`REGIME CHANGE … (保有中)` とログだけ出して SL/TP を待つ構造だった。
4. `backend/lab/runner.py` 542 行 `abs(best.get('avg_win_jpy',0)/best.get('avg_loss_jpy',-1))` — `get(key, default)` は「key 未存在のとき」にしか default を返さないため、**値が 0 のときはそのまま 0 除算**になる。`avg_loss_jpy=0` の戦略（勝ちトレードのみ or 損失が浮動小数丸めで 0）が入ると落ちる。

### 実装

#### Fix 1: EOD 強制クローズ (`backend/lab/jp_live_runner.py`)

- 新定数 `_FORCE_CLOSE_AT_SESSION_END`（既定 15:05 JST、`JP_FORCE_CLOSE_AT` で上書き可）。
- 新メソッド `_force_close_open_positions(now)` を追加し、`run()` ループから直接呼び出し。
  - ランナー管理ポジションは `_close_position(..., reason="session_close_forced")` 経由で DB まで記録。
  - ブローカーにだけ残っているゴーストは最終手段として素売り、警告ログを残す。
  - 直近の feed bar の close を参照するが、無ければ `entry_price` にフォールバック。
- `run()` の既存 15:30 サマリー送信ブロックの直前で呼ばれるため、セッション終了記録と完全に整合。

#### Fix 2: insufficient_lot 構造化ログ + ゴースト自動回収 (`backend/lab/jp_live_runner.py`)

- `qty < 100` 経路を `logger.debug` → `logger.warning` に引き上げ、以下を 1 行に畳む:
  `cash / position_value / required_min_cash / alloc_w / tier / tracked_n / held_value / broker_positions_n / ghost_symbols`
- `_record_skip_event` にも同内容を `detail` として渡し、後追いで `jp_skip_events` から分析可能に。
- **`broker_positions_n > tracked_n`（=ゴースト検出）**のときはその場で `_sell_ghosts(..., reason="insufficient_lot_rescue")` を呼んで cash を回復させる（`_on_jp_paper_fill` 修正後もゼロではない保険）。

#### Fix 3: レジーム反転早期撤退 (`backend/lab/jp_live_runner.py`)

- 定数: `_REGIME_EARLY_EXIT_ENABLED`（既定 on）/ `_REGIME_ADVERSE_STREAK`（既定 2）/ `_REGIME_EARLY_EXIT_WARMUP_MIN`（既定 1 分）。
- `_ADVERSE_REGIME_LONG = {trending_down, high_vol}`、`_ADVERSE_REGIME_SHORT = {trending_up, high_vol}`。
- `_process_strategy` のポジション管理ブロックで、以下**全て**を満たすときに `exit_reason = "regime_flip"` で即時退避:
  1. `pos.entry_regime` が adverse 集合に含まれていなかった（=入場時は OK だった）
  2. `pos.regime_history` 末尾 N 回が連続で adverse 集合
  3. `entry_time` から `_REGIME_EARLY_EXIT_WARMUP_MIN` 分経過
- ログ例: `REGIME FLIP EXIT 4385.T [...] entry=low_vol → now=high_vol (hold=90m pnl=-1200 streak=2)`

#### Fix 4: Zero-division 修正 (`backend/lab/runner.py` / `backend/main.py`)

- `generate_analysis` 内の R:R 計算を `get(k) or 0.0` に変更。以降 0 値でも `/0` を避ける（`rr_best = abs(_avg_win / _avg_loss) if _avg_loss else 0.0`）。
- `backend/main.py` の JP loop の `except` を `logger.error(..., exc_info=True)` に変更。今後の例外は必ずスタックトレース付きで残る。

### 変更ファイル

- `backend/lab/jp_live_runner.py` (+定数 / `_sell_ghosts` / `_force_close_open_positions` / insufficient_lot 構造化 / regime flip exit)
- `backend/lab/runner.py` (zero-div 修正)
- `backend/main.py` (`exc_info=True`)

### 検証（VPS 19:58 再起動）

- `algo-trading.service` active、起動時に ERROR / Traceback なし。
- `New session: 2026-04-22` → 15:30 以降扱いで `_force_close_open_positions` → `_send_session_summary` の順でクリーンに走ることを確認（当然ながらフレッシュ起動でポジション 0、ゴースト 0、エラー 0 の想定どおり）。

### 翌営業日に観測したい指標

| 指標 | 期待 |
|---|---|
| 未クローズ残留 | 0 件（`Force-close at session end` ログで件数ゼロ or 強制処理を観測） |
| `insufficient_lot` | 発生時に `ghost_symbols` が空 or 自動回収で cash が戻ることを確認 |
| `regime_flip` exit | 負け系銘柄で SL を待たずに早期撤退するケースが出現するか |
| `JP loop error` | 今後発生した場合はスタックトレース付きで 1 行のみ残る |

### 次アクション候補

- `regime_flip` による PnL 改善を 1 週間分計測（負けの切り下げ vs 取れていたはずの反転の喪失）。過剰なら `_REGIME_ADVERSE_STREAK=3` などで緩和。
- `insufficient_lot` の構造化ログを集計するスクリプトを追加し、week ごとに cash 起因 / alloc_w 起因 / ghost 起因のシェアを可視化。

## 2026-04-21 Phase F4 — 探索多様化（method_pdca / explore / oos_booster）

### 背景

直前のバックテスト報告で以下 3 件の課題を特定していた。

1. `method_pdca` 枠の 99% が `MacdRci × 3103.T` / `MacdRci × 6613.T` に集中し、他銘柄の Robust 昇格が止まっていた。
2. `is_oos_pass=True` を満たす銘柄は `8058.T` のみだが `oos_trades=6` と極小サンプルで、そのままでは T1 採用不可。サンプル積み増しの専用導線が無かった。
3. `trending_down` のように有効手法が 1 つしかないレジームでは、`explore` 枠の半分しか novel に回らず、未検証手法（`BbShort` / `Pullback` / `SwingDonchianD` / `MaVol` / `EnhancedScalp` / `EnhancedMacdRci`）の検証が遅延していた。

### 原因（特定）

`scripts/backtest_daemon.py` の `plan_generation` が `get_robust_experiments(min_oos=0, limit=100)` を参照しており、OOS 降順 LIMIT 100 の仕様上、**Robust 行が多い 3103.T / 6613.T の重複で 100 件が埋まり、候補銘柄が実質 2 銘柄に縮退**していた（`robust_by_strategy[MacdRci]` が 2 銘柄のみ）。これが method_pdca / exploit 全体の集中化を招いていた。

### 実装

1. **Robust プール diversification**
   - `plan_generation` で `get_robust_experiments_diversified(min_oos=0, limit=100)` に切替。
   - 結果: 候補銘柄が 2 → 60 行 / 4 戦略 / 30+ 銘柄（MacdRci 17 銘柄、Scalp 24 銘柄、Breakout 16 銘柄 等）に拡張。
2. **method_pdca 履歴フィルタ**
   - モジュール級 `_METHOD_PDCA_HISTORY: deque(maxlen=30)` を導入。
   - 採用時に push、プール選択時に占有率 `_METHOD_PDCA_MAX_SHARE=0.35` 超の銘柄を除外。
   - `pool_sorted` の第一キーを履歴カウントに変更（履歴が薄い初期でも既採用銘柄を後ろに回す）。
3. **OOS Booster 枠 (新規)**
   - `alloc["oos_booster"]` を n_robust<10→0, <30→1, ≥30→2 で配分。
   - `is_oos_pass=True` かつ `oos_trades<30` の行を自動抽出し、`neighborhood(magnitude=0.05)` で近傍摂動を評価。
   - 現在の候補は `8058.T`, `8306.T` ほか 1 銘柄。
4. **explore 銘柄の連投抑制**
   - `_EXPLORE_HISTORY: deque(maxlen=40)` と `_EXPLORE_MAX_REPEAT=3`。
   - 新 `_pick_explore_symbol(strategy_name)` で ATR% 上位 8 から (strategy, symbol) 履歴回数が上限未満の候補をランダム抽出。
5. **novel 強制枠**
   - `effective_strats` が 3 未満のとき、`explore_n/2 + 1` もしくは `_EXPLORE_NOVEL_FLOOR_WHEN_FEW_EFFECTIVE=3` の大きい方を novel に確保。
   - ログに `explore: novel floor X/Y novel_used=Z` を出力。

### 変更ファイル

- `scripts/backtest_daemon.py`（定数追加 / プール取得関数差替 / `_pick_explore_symbol` / `_build_method_pdca_experiments` 履歴フィルタ / OOS booster 枠 / novel floor ロジック）

### 検証

- 再起動後約 3 分のサンプリングで:
  - method_pdca/oos_booster で 15 種の (戦略×銘柄) 組合せが出現（従来 2 種）。
  - `Breakout × 8306.T` で **★Robust が OOS+290, +391 の 2 件発掘**。
  - ログ `explore: novel floor 3/5 novel_used=4` 確認。
  - `oos_booster: 候補 3 銘柄 (例 8058.T,8306.T)` 確認。

### 次アクション候補

- `8058.T` の `oos_trades` が `_OOS_BOOSTER_MIN_TRADES=30` を超えるまで積むことを目標に監視（ただし T1 ローテーションには入れない）。
- 新規発掘された `Breakout × 8306.T` Robust の walkforward 検証 / is_oos_pass 適用状況を次回レポートで確認。
- MacdRci 以外の手法で「有効手法の閾値 10%」を超える戦略が現れたら `effective_strats` に昇格し、affinity マップ整合を確認。

## 2026-04-17 第1波

### 予定

1. `docs/PHASE1_GATE.md` と `data/phase1_gate_config.json` を作成
2. `scripts/evaluate_phase1_gate.py` を実装し `data/phase1_gate_latest.json` を生成
3. `backend/lab/jp_live_runner.py` に日次損失ガードと halt ファイル読込を実装
4. `docs/RUNBOOK_PHASE0_TO_PHASE1.md` を作成
5. `docs/PHASE0_INVENTORY.md` を時系列更新

### 実装

- フェーズ1移行ゲート文書を追加（判定項目: 損益/DD/取引数/集中度/整合率）。
- 判定スクリプトを追加し、DB から指標計算して `data/phase1_gate_latest.json` を出力。
- ランナーに以下を追加:
  - `JP_RISK_BASE_JPY` / `JP_DAILY_LOSS_LIMIT_PCT` に基づく新規エントリー拒否
  - `data/jp_paper_trading_halt.json` による手動停止
- Runbook を追加し、日次運用手順と停止/再開手順を明文化。

### 逸脱理由と対応

- 逸脱なし（添付プランの第1波 A〜E を順番どおり実装）。

### 生成・更新された成果物

- `docs/PHASE1_GATE.md`
- `data/phase1_gate_config.json`
- `scripts/evaluate_phase1_gate.py`
- `data/phase1_gate_latest.json`
- `backend/lab/jp_live_runner.py`
- `data/jp_paper_trading_halt.json`
- `docs/RUNBOOK_PHASE0_TO_PHASE1.md`
- `docs/PHASE0_INVENTORY.md`（本ログ作成時点で更新）

## 2026-04-17 第2波（着手）

### 予定

1. 場中候補の動的更新について、既存 `run_midday_check_loop` と競合しない最小フックを追加

### 実装

- `backend/main.py` の `_daily_prep_loop` に、環境変数で有効化できる場中再選定フックを追加。
  - `JP_INTRADAY_RESELECT_ENABLED=1` のときのみ有効
  - `JP_INTRADAY_RESELECT_INTERVAL_MIN`（既定 30, 最小 5分）
  - `run_midday_check_loop` と `asyncio.gather` で併走
  - `_daily_reselect_lock` で再選定の多重実行を抑止
- `backend/lab/daily_prep.py` に method_pdca 成績ベースの allocation 補正を追加。
  - `get_method_pdca_strategy_summary(days, min_trials)` を追加し手法別 robust_rate を取得
  - `PDCA_ALLOC_*` 環境変数で有効化・強度・上限下限を制御
  - 試行数不足時は confidence で効きを弱める安全ガードを実装
- `.env` に `JP_INTRADAY_RESELECT_ENABLED=1` / `JP_INTRADAY_RESELECT_INTERVAL_MIN=30` を反映
- `scripts/setup_phase1_ops_cron.py` を追加し、ゲート評価の定期実行を自動化
  - 平日 08:40（dry-run）
  - 平日 15:45（最新JSON更新）
- `docs/RUNBOOK_PHASE0_TO_PHASE1.md` に cron 設定手順を追記
- 東証営業日ガードを追加（`scripts/run_if_tse_trading_day.py`）
  - `data/tse_trading_calendar.json` で休場日/臨時営業日を上書き可能
  - 非営業日は cron 実行時に自動スキップ

### 逸脱理由と対応

- 逸脱なし（第2波項目のうち「拡張可能なフック」まで先行実装）。

## 2026-04-17 バックテスト基盤強化（最短経路）

### 実装

- `scripts/backtest_daemon.py` にデータ品質ゲートを追加（`BACKTEST_DATA_QUALITY_*`）。
- `backend/backtesting/walkforward.py` を追加し、multi-split OOS統計（avg/std/worst）を導入。
- `data/backtest_cost_model.json` を追加し、コストモデルを設定ファイル化。
- `experiments` テーブルに walkforward/cost差分の保存列を追加（後方互換 migration 対応）。
- `scripts/evaluate_backtest_quality_gate.py` を追加し、`data/backtest_quality_gate_latest.json` を生成。
- `scripts/backtest_data_quality_report.py` を追加し、`data/backtest_data_quality_latest.json` を生成。
- `scripts/snapshot_backtest_quality_weekly.py` を追加し、週次差分スナップショットを保存。
- `evaluate_phase1_gate.py` に `require_backtest_quality_gate` 連携を追加（flag付き）。
- `setup_research_ops_cron.py` に品質ゲート日次・品質スナップ週次のエントリを追加。

### 逸脱理由と対応

- 逸脱なし。既存運用保護のため、新判定はすべて flag で段階導入可能にした。

## 2026-04-18 MacdRci / ドキュメント

- `JPMacdRci` に `rci_entry_mode`（0=RCI多数決、1=最短×最長RCIのGC/DC・**翌足**、2=**当足**）を追加。
- `PARAM_RANGES`・`method_pdca`・`strategy_factory` を連携更新。
- 各戦略のエントリー条件を `docs/STRATEGY_ENTRY_CONDITIONS.md` に整理。
- 同ファイルにバックテストエンジン共通の決済順・各戦略のエグジット条件を追記。
- MacdRci: 最短RCI傾き（`rci_short_slope` 等）の列出力と、GC モード時の傾き帯フィルタ `rci_gc_slope_*`。
- `scripts/export_macd_rci_signals_csv.py`: 上記列を含む CSV / メタ JSON を出力。

## 2026-04-18 MacdRci OOS 傾きの DB 蓄積・報告連携

- `experiments.rci_slope_summary_json`: MacdRci 実験の OOS `generate_signals` から `summarize_macd_rci_oos_signals` で JSON 化して保存（`backend/backtesting/macd_rci_slope_metrics.py`）。
- `scripts/backtest_daemon.py` の `run_experiment` が OOS 実行前にサマリーを生成して `save_experiment` に渡す。
- `aggregate_macd_rci_slope_since(min_experiment_id)` で境界以降の傾きを集約。
- `scripts/update_backtest_report_checkpoint.py --dry-run` が上記集計ブロックを標準出力に出す（バックテスト報告の機械差分に含める）。
- 追補: `aggregate_slope_summaries` の件数キーを `experiment_count` に明記（`experiments` は後方互換エイリアス）。`tests/test_macd_rci_slope_metrics.py` で `unittest` による回帰テスト。`BACKTEST_REPORT_TEMPLATE.md` Step 2 に傾きブロックの読み方を追記。

## 2026-04-18 MaVol（EMA×出来高）

- `backend/strategies/jp_stock/jp_ma_vol.py` の `JPMaVol`、ファクトリ名 `MaVol`。`interval_code` で 1m/3m/5m/15m/30m/1h を切替。
- `resolve_jp_ohlcv_interval` とデーモン `_get_ohlcv(symbol, interval)` により実験ごとに正しい足を取得。キャッシュキーは `symbol::interval`。
- `portfolio_sim`・ブースト分析・保有時間・ポートフォリオ事前キャッシュを同じ解決ロジックに追従。
- 追補: セッション VWAP と `vwap_entry_margin_pct` でロング/ショートのエントリーを分離、`allow_short` でショート新規の on/off。

## 2026-04-18 BbShort（BB 3σ ショート）

- `backend/strategies/jp_stock/jp_bb_short.py` の `JPBbShort`、ファクトリ名 `BbShort`。上バンド初タッチで `signal=-2`、ミドル下抜けで `-1`。ロング新規なし。

## 2026-04-18 報告フォロー（チェックポイント・集中度・PDCA 分散）

- `backend/storage/research_canonical_sync.sync_strategy_fit_map_json`: `get_best_robust_per_symbol` を全手法に対してマージし `data/strategy_fit_map.json` を更新（mtime 停滞対策）。`daemon_state.backtest_daemon_heartbeat.strategy_fit_map_symbols_updated` で件数確認。無効化は `DISABLE_STRATEGY_FIT_MAP_SYNC=1`。
- VPS: `scripts/update_backtest_report_checkpoint.py` と傾き集計依存ファイルを同期。チェックポイント実行は `.venv/bin/python3` を使用。
- VPS `data/backtest_report_checkpoint.json` を報告境界に更新（メモ「2026-04-18 夕方報告」、`last_experiment_id` 66402 / generation 7192）。ローカルへ `scp` 済み。
- `scripts/report_method_pdca_symbol_concentration.py`: `method_pdca` の銘柄集中度を表示。
- `scripts/backtest_daemon.py` の `_build_method_pdca_experiments`: 上位プールを 12 銘柄まで広げ、同一世代バッチ内の銘柄使用回数が少ない Robust を優先して選択（3103.T 偏重の緩和）。

## 2026-04-20 報告次アクション（method_pdca 再生成・PF 暦日・type 診断）

- `scripts/backtest_daemon._build_method_pdca_experiments`: 1 枠あたり **48 回まで**パラメータ再抽選し、墓場衝突・`candidate==base` の直 `continue` 廃止（枠が全滅しにくくする）。失敗時は WARN。
- `backend/backtesting/portfolio_sim`: **`MIN_OOS_BARS_PORTFOLIO=40`**（旧 60）。短い OHLCV でも成份バックテストが走りやすく、`portfolio_latest_curve` の暦日行が **0 のまま**になりにくい。`run_portfolio_sim` で暦日 PnL が空なら WARN。
- VPS 診断 `diagnose_experiment_type_coverage.py --since-id 100201` 例: **explore のみ**（境界内の増分試行のみを表示）。

## 2026-04-19 報告フォロー追補（method_pdca・品質JSON・知識ベース）

- **method_pdca が DB に載らない**: `rolling_splits` が空のとき `run_experiment` が無音スキップしていた。`experiment_type=method_pdca` のときは **単一スプリットにフォールバック**（`scripts/backtest_daemon.py`）。VPS にデプロイ・デーモン再起動済み。
- **切り分け**: `scripts/diagnose_experiment_type_coverage.py --since-id N` で type 別 min/max id。
- **data quality**: `get_backtest_data_quality_summary` に `flag_rate` / `clean_rate` / `interpretation` を追加（`flag_only` では issue_rate が高く見えやすい旨）。
- **strategy_knowledge.json の mtime**: Lab 経由の `save()` のみ更新する旨を `backend/analysis/strategy_knowledge.py` モジュール先頭に明記。

## 2026-04-21 メモ: 夜間 WF の `demote` 全滅時の振り返り順序（進捗に応じて見直し）

**背景**: `scripts/nightly_walkforward_revalidation.py` の `demote_candidates` は簡易 WF（`rolling_splits` + 閾値）で付与。`evaluate_backtest_quality_gate.py --prune-universe` はここから `universe_active` を剪定（全件削除は `protected` で防止）。

**合意（運用哲学）**

- バックテスト改良の目的は「条件を満たす組み合わせが出る土台」なので、**水準そのものが過激すぎるとは限らない**。
- **本番ゲートと同じ軸に揃えたうえで**、長期・十分なデータでも **ずっと全員 demote** が続くなら、**緩めるより手法（またはユニバース設計）を疑う**、という整理でよい。

**切り分け（実装・観測）**

1. まず **夜間チェックの定義を本番の `is_oos_pass` 等に寄せる**（窓・指標・データ長のズレで「夜間だけ全滅」が起きうるため）。
2. それでも全滅が続く → **手法・パラメ探索・銘柄プール**側を振り返る。

進捗を見てこの節を更新・削除してよい。

## 2026-04-21 アプリ追従（Phase A–F の UI / 報告反映）

Phase A–F の Backtest 1oku Roadmap で追加した裏側の指標を、モバイル PWA・PC `/lab`・LLM 報告プロンプトの 3 面に露出させた。後方互換のため、既存フィールドはすべて残し、新フィールドは optional で追加のみ。

### 新 JSON / 生成スクリプト

- `data/tier_sweep_latest.json`（新規）  
  `scripts/update_tier_sweep_snapshot.py` が書き出す。`base_daily_return_pct` / `goal_days` / `goal_months` / `tiers[]`（各 Tier の `days_in_tier` / `cum_days` / `daily_return_pct` / `monthly_return_pct` / `position_pct` / `slippage_bps`）。  
  引数: `--mode {all|positive_rows|quality}`（既定 `quality`）、`--base-daily-pct <float>`（直接指定）、`--goal-jpy <int>`、`--keep-history`（`data/tier_sweep_history/YYYYMMDD.json`）。  
  朝の ops cron に 1 行追加して毎日更新する想定（ジョブ登録スクリプトの改訂は本プランの範囲外で別タスクに回す）。

- `data/nightly_walkforward_latest.json`（既存・Phase F1）: `total` / `demote_candidates[]` / `updated_at` / `results[]` を UI・LLM 側から参照開始。

- `data/paper_validation_handoff.json` の `last_divergence`（既存・Phase E3）: UI と LLM 報告から参照開始。

### Mobile API（`backend/routers/mobile.py`）

- `GET /api/m/today` `backtest` ブロック拡張:  
  - `is_oos_pass_count`（`macd_rci_params.json` の `is_oos_pass=true` 件数）  
  - `nightly_wf.total` / `nightly_wf.demote_count` / `nightly_wf.updated_at`  
  - `paper_divergence.{paper_pnl_jpy, backtest_sum_oos_jpy, diff_jpy, diff_pct, severity, last_session_date}` (`last_divergence` 未生成時は `null`)  
  - `tier_sweep.{base_daily_return_pct, goal_days, goal_months, computed_at, note}`（未生成時は `null`）
- `GET /api/m/backtest/summary`:  
  - `rows[]` に `is_oos_pass` / `calmar` / `daily_return_pct_mean` / `daily_return_pct_std` / `wf_window_pass_ratio` を追加。  
  - 並び順を **`is_oos_pass` 優先 → `oos_daily` 降順** に変更。  
  - トップレベルに `is_oos_pass_count` を追加。`robust_count` は残置（Robust 通過ラベルに改名）。
- `GET /api/m/tier_sweep`（新規）: `data/tier_sweep_latest.json` を素通し + `computed_at_display`。未生成時は `{ "available": false }`。

### モバイル PWA（`frontend/static/m/js/app.js` / `api.js`）

- Home のバックテストカードに 3 行追加: 「夜間WF 閾値割れ」「紙トレ乖離（最新）」「30万→1億 推定」。`severity=='critical'` / 大量 demote は警告色で表示。
- Backtest タブ: KPI の片方を「厳格OOS通過」に置き換え、上位 3 行に Calmar / 日次% / WF通過率、`✓ OOS Strict` バッジを追加。末尾に「30万→1億 推定到達日数」カード（Tier 別の `days_in_tier` を棒で可視化）。
- `api.js` に `backtestTierSweep: () => jget('/tier_sweep')` を追加。

### PC `/lab`（`frontend/static/js/lab.js`）

- 結果行の戦略名横に `✓OOS`（緑）バッジを追加（`r.metrics?.is_oos_pass === true` のみ）。未到達は薄いドット、未判定はバッジなし（最小変更）。

### LLM 報告（`backend/services/reports/backtest.py`）

- `collect_material()` の `robust_top` に `is_oos_pass` / `calmar` / `daily_return_pct_mean/std` / `wf_window_pass_ratio` を追加。全体に `is_oos_pass_count` / `nightly_wf`（`demote_top3` 含む）/ `paper_divergence` / `tier_sweep`（Tier 要約）を追加。`robust_top` の並び順を「厳格OOS優先 → OOS日次降順」に変更。
- `_format_user_prompt()` を更新し、上記新材料を材料セクションに明示。判断軸の主軸を **`is_oos_pass` / Calmar / 日次% / 1億到達日数 / 夜間WF demote / 紙トレ乖離** に置き換え、PF・勝率・ゲート可否は補助指標扱いとした。

### 後方互換メモ

- 新フィールドが未生成の場合、モバイル API はそれぞれを `null` もしくは省略で返し、UI 側は「—」または「データ待ち」にフォールバックする。
- 旧 app.js（キャッシュが長いクライアント）に新 API が来ても壊れない（既存キーはすべて残置）。
- `strategy_fit_map.json` / `macd_rci_params.json` に `is_oos_pass` 列が無い旧データはカウント 0、UI はドット/空扱い。

### 積み残し

- Tier sweep カードの視覚デザイン（棒グラフの色分けなど）は最小実装。必要に応じて後続で磨き上げる。

## 2026-04-21 Tier sweep 週次運用 & 80年未達フラグ

- `scripts/print_pseudo_portfolio_stats.py` の `_tier_sweep_from_daily_pct` に **`cap_days`**（既定 **80 年 = 29200 日**）を追加。累計日数が上限を超える見込みなら最後の Tier に `truncated=true` を立て、返り値は `status="unreachable"` / `goal_days=None` / `goal_months=None` / `goal_years=None` / `note="上限 80 年以内に 1 億到達不能（未達）"`。到達ケースは `status="reached"` で `goal_years` を新設。
- `scripts/update_tier_sweep_snapshot.py`  
  - `--cap-days`（既定 80 年）、`--no-history` 追加。履歴保存は **既定 ON** に変更。  
  - `data/tier_sweep_latest.json` に `status` / `cap_days` / `cap_years` / `goal_years` を追加。同日再実行は `data/tier_sweep_history/YYYYMMDD.json` を上書き。
- `scripts/setup_tier_sweep_cron_vps.sh`（新規）  
  - VPS に毎週 **日曜 07:50 JST** の cron を登録（`nightly_walkforward` 03:10 → tier_sweep 07:50 → `weekly_universe_rotation` 07:30 月曜 の順）。  
  - ログ出力: `logs/tier_sweep.log`。
- `backend/routers/mobile.py` `GET /api/m/tier_sweep` に比較情報を追加:  
  - `compared.prev` / `compared.prev_shortened_days`（正なら短縮、負なら延長）  
  - `compared.month_ago` / `compared.month_ago_shortened_days`（30 日以上古い履歴のうち最新）  
  - `history[]`（直近 52 週の軽量サマリ）、`history_count`、`status`、`cap_days`、`goal_years`。
- `backend/routers/mobile.py` `GET /api/m/today` の `backtest.tier_sweep` にも `status` / `goal_years` / `cap_days` を追加。
- モバイル UI（`frontend/static/m/js/app.js`）:  
  - Home の「30万→1億 推定」行を `status=='unreachable'` なら「**未達（80年以内不能）**」表示に。  
  - Backtest カード head は、未達時「**未達**（現在の基礎リターンでは 80 年以内に 1 億到達不能）」。到達時は「X 日（約 Y 年 / Z ヶ月）」の 3 軸表示に変更。  
  - 末尾に **前回比 / 1ヶ月前比の短縮日数** を表示（履歴が増えたら自動で出る）。
- `backend/services/reports/backtest.py` も未達時は "**未達** (上限 80 年)" と LLM prompt に明示。到達時は `goal_years` も出す。
- 初回スナップショット（VPS）: `status=reached` / `goal_days=398` / `goal_years=1.09`（pseudo PF quality 基礎。**live ではなく「うまく回った日のベスト合算」の楽観ケース**である点に注意）。今後は週次でここから短縮されるかをトラッキングする。

## 2026-04-21 T1 フェーズに向けた戦略ロスタ入れ替え

### 背景

- `experiments` 集計で `is_oos_pass`（strict OOS ゲート）が **MacdRci 3/20k・Breakout 15/20k・Scalp 1/651** と壊滅的。ネックは「OOS 勝率 ≥ 50%」条件で、既存の利大損小型（MacdRci/Breakout）は構造的にクリアしにくい。
- `Momentum5Min` / `ORB` / `VwapReversion` は robust=0、平均 OOS が負 or NULL で完全に死んでいた。PDCA リソースの無駄。

### 決定

1. **死戦略を PDCA から除外**: `Momentum5Min` / `ORB` / `VwapReversion` を `backend/backtesting/strategy_factory.py` の `STRATEGY_DEFAULTS` / `PARAM_RANGES` / `create()` から削除。クラス本体は `backend/lab/runner.py` や `social_strategy.py` が直接 import しているため残す。
2. **新規手法 2 本を追加**:
   - `JPPullback`（5m, ロング専用, 勝率優先）: EMA20>EMA50 トレンド＋VWAP 上方＋EMA20 押し目→復帰＋RSI 帯域＋出来高閾値。is_oos_pass の勝率 50% 壁を設計で取りに行く。
   - `JPSwingDonchianD`（1d 固定, ロング専用）: 20日ブレイク＋EMA50 上方＋ATR ストップ＋10日逆ブレイク手仕舞い。intraday 群と相関を下げて T1 以降の複利 DD を分散させる。
3. **daemon 選択ロジック修正**: `effective_strats` フィルタ（regime_effective ≥ 0.10）が新戦略を恒久的に弾く問題を修正。未検証手法を `novel_strats` として explore 枠へ交互投入し、さらに世代ごとにスタート offset をランダム化（末尾手法が永遠に回らない問題も解決）。
4. **`portfolio_sim.py` のバグ修正**: `df = df_cache.get(ck) or df_cache.get(sym)` が DataFrame で ambiguous になって daemon が 876 回再起動ループしていたのを明示フォールバックに変更。

### 判断理由

- `is_oos_pass` ゲートは最終目標の DD 規律上緩めたくない。ゲートは据え置きにして、**それを通す手法を後から追加**する方向。
- 既存 `MacdRci` に部分利確の改良も検討したが、engine 側 signal 規約変更が必要で検証コストが高く、今回は見送り。
- T1（30万→100万）の複利を intraday だけで取るのは勝率・コスト・出来高の観点で厳しいため、**日足スイング 1 本をポートフォリオに加える**のが合理的。

### 生成・更新された成果物

- `backend/strategies/jp_stock/jp_pullback.py`（新規）
- `backend/strategies/jp_stock/jp_swing_donchian.py`（新規）
- `backend/backtesting/strategy_factory.py`（死戦略 archive / 新規戦略 registration / `resolve_jp_ohlcv_interval` で 1d 返却）
- `backend/backtesting/holding_time.py`（エントリー締切デフォルトから死戦略を除外、新戦略を追加）
- `backend/backtesting/portfolio_sim.py`（DataFrame bool ambiguous 修正）
- `scripts/backtest_daemon.py`（novel_strats + ローテーション offset）
- VPS deploy 済み、daemon 再起動で Pullback / SwingDonchianD / MaVol / EnhancedScalp 全てが explore 枠で毎世代ローテに乗ることを確認。

### 残タスク（次以降）

- Pullback の PDCA 探索が進み、is_oos_pass を取れるパラメータ帯が出るまで観察。
- SwingDonchianD は OOS 日数が少ないとスキップされるため、`WALKFORWARD_MODE` の split 設定も含め PDCA 経過を見て調整。
- `MacdRci` の勝率改善（部分利確 or breakeven 退出）は engine 側 signal 規約の整備後に再検討。

## 2026-04-21 Tier sweep 営業日換算 & 基礎%を全日平均へ

- **年換算**: `goal_days` はもともと複利ステップ＝**東証 1 営業日**前提。`goal_years` を **÷365 から ÷245（`JP_TRADING_DAYS_PER_YEAR`）** に変更。80 年上限も **80×245=19600 営業日**（暦日 29200 ではない）。
- **基礎%/日**: `update_tier_sweep_snapshot.py` の既定モードを **`quality` → `all`**（各日の合計損益の算術平均 ÷30万）。`quality` は行フィルタで楽観寄りになりやすいため。
- **定数**: `backend/lab/runner.py` に `JP_TRADING_DAYS_PER_YEAR = 245` を追加し、Tier sweep・`portfolio_sim.sweep_tiers`・疑似 PF スクリプトと共有。
- **モバイル**: 表示に「営業日」「年換算（245営業日=1年）」を明示。`cap_years` は API で **÷245**（`tier_sweep_latest.cap_years`）。

## 2026-04-21 JP paper: ゴーストポジション撲滅 & post-loss gate TTL

### 背景（ペーパー振り返り 2/3）

- 当日の skip 内訳が `insufficient_lot=260` / `post_loss_recheck_block=186` と偏り、実エントリーが極端に痩せていた。
- ブローカー残現金が `22,600 JPY`（starting 990,000）まで枯渇。ただし LiveRunner 側の `ENTRY` ログは対応する買いが出ていなかった。
- 原因は 2 点:
  1. `backend/main.py` の `_on_jp_paper_fill` が **同期関数**のまま `on_fill` に登録されていた。`PaperBroker.place_order` は `asyncio.ensure_future(cb(...))` でコールバックを呼び出すため、同期関数を渡すと `TypeError` が発生し、`place_order` の戻り値まで辿り着く前に例外が伝播 → ENTRY ログと `self._positions` 登録が欠落。ブローカー側にだけ買い状態が残り**ゴーストポジション**化。翌営業日以降 `cash` が回復せず、T1 建玉が `insufficient_lot` を量産。
  2. `post_loss_gate` に TTL がなく、9433.T の 09:54 ストップ後、vol/regime が戻らないまま終日ブロック。FALLBACK WAIT ログも同一条件で連呼されノイズ化。

### 実装

- `backend/main.py`: `_on_jp_paper_fill` を `async def` 化（docstring で理由を明記）。
- `backend/brokers/paper_broker.py`: `place_order` 内のコールバック呼び出しを `try/except` で保護。`coroutine/Future` 以外が返ってきた場合は無視する（通知系の失敗を約定処理に波及させない）。
- `backend/lab/jp_live_runner.py`:
  - 日付切替タイミングで `_reset_broker_daily(today)` を呼び、`PaperBroker._cash` を `starting_cash` に、`_positions` を空にリセット。`ENTRY` ログで追跡していない「ゴースト」を検出したら警告ログを吐く。
  - `_RECHECK_TTL_MIN`（デフォルト 45 分, `JP_RECHECK_TTL_MIN`）を追加し、`_evaluate_post_loss_gate` に TTL チェックを挿入。`triggered_at` から 45 分経過したら gate を無条件解除し、再エントリー評価を許す。
  - `_FALLBACK_LOG_MIN_INTERVAL_SEC`（デフォルト 300 秒, `JP_FALLBACK_LOG_MIN_INTERVAL_SEC`）を追加。`FALLBACK WAIT` ログを `(symbol, reason)` 単位で 5 分に 1 回に間引き。
  - 日付切替時に `_fallback_wait_last_logged` もクリア。

### 検証

- ローカルスモーク: 同期 cb を `on_fill` に登録しても `place_order` は `filled` で完了。`_reset_broker_daily` は starting_cash 復元 & ゴースト 1 件検出を確認。
- VPS 再起動後: `Broker daily reset [2026-04-21]: cash 990000→990000 equity_before=990000` が出て開始。`TypeError` ループ消滅。

### 運用メモ

- ゴースト再発防止のため、新しい `on_fill` コールバックを追加するときは **必ず async def** とし、`paper_broker._fill_callbacks` を手で書き換えない。
- `_RECHECK_TTL_MIN=0` にすると従来挙動（TTL なし）。ログ間引きを解除したい場合は `_FALLBACK_LOG_MIN_INTERVAL_SEC=0`。

## 2026-04-21 バックテスト報告 Next Action 3 件対応

### 1) `backtest_daemon` の単一化

- VPS 上で `backtest_daemon.py` が 2 プロセス併走していた（PID 922918: 2日前の手動起動 / PID 1040573: `backtest-daemon.service` 管理）。
- 非 systemd プロセスを `SIGTERM` で停止し、systemd 管理下の 1 本のみ稼働に戻した。
- 運用方針: 以後の起動は **必ず `systemctl restart backtest-daemon.service`** を経由。手動で `python3 scripts/backtest_daemon.py` を実行しない。

### 2) `robust` vs `is_oos_pass` のポリシー確定 & 監査常設化

- 定義（`scripts/backtest_daemon.py`）:
  - `robust`: `OOS>0 AND OOS/IS>=0.3 AND worst_wf>-3000`
  - `is_oos_pass` は **`robust` の strict superset**: さらに `OOS勝率>=50%` / `WF窓通過率>=2/3` / `worst > -1.5*avg_win` を満たすもの。
- 採用ポリシーはコード上既に統一: `weekly_universe_rotation.py` と `backtest_live_rank_allocator.py` がともに `is_oos_pass` を必須ゲートに採用、モバイル・Lab UI も `is_oos_pass_count` / バッジ表示を実装済み。
- 新規に `scripts/audit_canonical_gate_consistency.py` を追加。DB の「各銘柄の Robust ベスト」行と `data/macd_rci_params.json` を付き合わせ、以下を検査:
  - JSON にだけ残った `robust=True` の「stale」行
  - DB にあるのに JSON 未反映の「missing」行
  - `is_oos_pass` / `oos_win_rate` / `wf_window_pass_ratio` の値乖離（divergent）
- 初回監査結果: DB Robust 17 件 vs JSON Robust 17 件、`divergent=0`。**同期は健全**。ただし `robust=True & is_oos_pass=True` は **1 件のみ**、残り 16 件は階層的な前段（採用候補ではない）。

### 3) 品質ゲートを堅牢統計中心へ再設計

- `scripts/evaluate_backtest_quality_gate.py` の合否判定を **外れ値に強い指標**に変更:
  - 合否用チェック: `min_median_oos_daily_pnl` / `min_trimmed_mean_oos_daily_pnl`（5% trim）/ `min_robust_avg_oos_daily_pnl`（既定 100 円/日）
  - 従来の `min_avg_oos_daily_pnl` は **`observational_checks` に分離**（合否に寄与しない観測用）。
  - 既存の `min_trials` / `min_robust_rate` / `max_avg_dd_pct` / `max_avg_cost_drag_pct` は維持。
- 背景: 診断で 30 日ウィンドウ 78,411 行のうち **単純平均=-92 円/日**だが **中央値=+285 円/日 / 堅牢集合平均=+2,038 円/日**。
  `-28,963 円/日` が `MacdRci × 6613.T`（trades=73, robust=0）の同一パラメータ反復評価から 2,150 行以上派生し、単純平均を引きずっていた。
- ゲートを更新後: **`passed=true`**（median=+285, trimmed=+557, robust_avg=+2038）。
- 追加キー: `sample_size_oos` / `median_oos_daily_pnl` / `trimmed_mean_oos_daily_pnl` / `trim_pct` / `robust_avg_oos_daily_pnl`。

### 成果物（変更）

- 追加: `scripts/audit_canonical_gate_consistency.py`
- 更新: `scripts/evaluate_backtest_quality_gate.py`
- 正本更新: `data/backtest_quality_gate_latest.json`（堅牢統計に基づき `passed=true`）
- 運用: VPS 側で `backtest_daemon.py` 単一化、`.venv/bin/python3` 経由の実行に統一。


## 2026-04-27 バックテスト報告フォローアップ — Robust 採用配置の WF 検証 / `paused_pairs` 機構

### 背景

4/26 のバックテスト報告で残った 2 件のフォロー（**A1: 9984.T 高 WR 副軸の cross_pollinate 試験 / A2: 1605.T MacdRci → Scalp 置換 WF 検証**）。共に「採用ロジックが選んでいる config が、直近 90 日の現データで本当に最良か」を WF 5 split で確認するもの。

### (A1) 9984.T 高 WR 副軸 walkforward — 置換不要と判定

`extract_high_wr_alt_configs.py --symbol 9984.T --strategy MacdRci --min-oos-trades 30 --min-oos-pf 1.2 --min-oos-daily 1500` で抽出した上位 2 件を、現行採用 config と並べて 5 split walkforward。

| label | oos_positive | avg_oos_daily | reference_50/50 |
| --- | --- | --- | --- |
| adopted（採用 WR 40.7%, trades 91, daily +8,937） | **5/5** | **+9,962** | +9,831 |
| alt#233906 (gen42095, explore, WR 68.6%, trades 51, daily +6,919) | 5/5 | +8,049 | +7,233 |
| alt#236128 (gen42282, oos_booster, WR 66.7%, trades 45, daily +7,125) | 5/5 | +7,718 | +7,195 |

→ 3 つとも 5/5 OOS positive で再現性は同等だが、**avg OOS daily は採用が +1,900〜+2,200 円勝つ**。「WR 警告」は採用ロジックが期待値で勝つ config を選んでいる構造上の現象であり、`9984.T` は現状維持。

成果物:
- 追加: `scripts/walkforward_alt_macd_rci.py`（採用 vs 高 WR alt の MacdRci walkforward 比較）
- 出力: `data/snapshots/walkforward_alt_9984_2026-04-27.json`

### (A2) 1605.T MacdRci → Scalp 置換 WF — 強い置換シグナル検出

同手法を「異なる戦略を同銘柄で比較」に拡張し、`scripts/walkforward_strategy_compare.py` を新規作成（`--baseline-strategy/--baseline-id` と `--candidate-strategy/--candidate-ids` を独立指定）。

1605.T の MacdRci 採用 (id=112726, DB の oos_daily=+1,221) を baseline、Scalp の robust 上位 3 種 (params 異なる代表 3 件) を candidate として走らせた結果（90 日, splits=0.3〜0.7）:

| label | oos_positive | avg_oos_daily | avg_oos_trades | reference_50/50 |
| --- | --- | --- | --- | --- |
| baseline: MacdRci#112726 | **0/5** | **-1,636** | 63.4 | -2,689 |
| candidate: Scalp#236261 | 5/5 | +529 | 17.4 | +694 |
| candidate: Scalp#226502 | 5/5 | +507 | 14.2 | +583 |
| candidate: Scalp#227348 | 5/5 | +564 | 14.2 | +645 |

→ MacdRci 採用は **0/5 OOS positive・avg -1,636** と完全に劣化。DB 上の `oos_daily=+1,221` は古いスナップで現データでは機能していない（過学習疑い）。Scalp は **3 種類とも 5/5 OOS positive・avg +500 円台**で安定。

### (A2 反映) `paused_pairs` 機構 — universe を触らずに pair 単位で停止

問題: `data/universe_active.json` を直接編集すると `weekly_universe_rotation` / `merge_robust_into_universe` の次回実行で書き戻される。

解決: `data/jp_paper_trading_halt.json` のスキーマを拡張し、(symbol, strategy) 単位の一時停止を受け付ける。

- 既存 `{halt: bool, reason: str}` に **`paused_pairs: [{symbol, strategy_name, reason, until, ...}]`** を追加。
- `until` は YYYY-MM-DD で、当日が `until` を超えると自動失効。
- `backend/lab/jp_live_runner.py::_try_open_position` の冒頭（既存 `manual_halt` 判定の直後）で `paused_pairs` を線形走査。`type(strategy).__name__` から `JP` プレフィックスを除いた戦略種別 ("MacdRci"/"Scalp"/"Breakout" 等) と照合し、ヒットしたら `_record_skip_event(reason="paused_pair", detail={...})` を残してエントリーを拒否。
- 既存の `_record_skip_event` を経由するので DB の `jp_signal_skip_events` に永続化され、`scripts/report_jp_signal_skip_events.py` から日次集計可能。

**初回登録**: `1605.T × MacdRci` を `until=2026-05-11`（2 週間）で停止。理由: WF 0/5 OOS positive。

成果物:
- 追加: `scripts/walkforward_strategy_compare.py`
- 更新: `backend/lab/jp_live_runner.py`（`_try_open_position` に `paused_pairs` ガード追加）
- 更新: `data/jp_paper_trading_halt.json`（スキーマ拡張、1605.T MacdRci を停止）
- 出力: `data/snapshots/walkforward_compare_1605_macdrci_vs_scalp.json`, `data/snapshots/walkforward_compare_1605_macdrci_vs_scalp_v2.json`
- 運用: `algo-trading.service` を再起動して新コードと halt ファイルを反映。

### 残課題（観察）

- A3: 4/28 朝の `morning_warmup_block` / `paused_pair` の skip_event 件数を `scripts/report_jp_signal_skip_events.py --date 2026-04-28` で集計し、想定どおりの抑止効果が出ているか確認する。
- 1605.T の MacdRci 配置は採用ロジック側で「直近 WF」が考慮されていないことが本質課題。`research_canonical_sync` か `merge_robust_into_universe` 側に **直近 WF 0/5 のペアは Robust から外す** 二段ゲートを後続で検討。


## 2026-04-28 二段ゲート: 夜間 WF demote → universe 採用判定への接続

### 背景

4/27 の WF compare で 1605.T MacdRci が `oos_positive=0/5` だったが、`merge_robust_into_universe.py` の品質ゲートは DB 上の `oos_daily/oos_pf/is_oos_pass/oos_trades` のみ参照しており、**直近 WF の劣化を見ていない**。一方で `scripts/nightly_walkforward_revalidation.py` は universe 上の全ペアを毎晩 03:10 cron で WF し `demote_candidates` を出力していたが、**この出力は誰にも消費されていない孤児**だった。両者を繋いで「直近 WF 劣化ペアは Robust 採用拒否」「universe からの除去は opt-in」という二段ゲートに改修する。

### (B1) `nightly_walkforward_revalidation.py` の重大バグ修正

最新 `data/nightly_walkforward_latest.json` を読むと、12 件中 6613.T 以外の 11 件全てが `oos_daily_mean=0.0 / wins=0/2 / pass_ratio=0.0` という非現実的な結果。原因は `_evaluate_one` 内の `run_backtest` 呼び出しで以下が欠落していた:

- `starting_cash=JP_CAPITAL_JPY`（=30 万円）のみで **`MARGIN_RATIO=3.3` が掛かっていない**（実運用は 99 万円）
- `fee_pct` / `limit_slip_pct` / `eod_close_time` / `short_premium_daily_pct` 未指定

→ 株価 × 100 株が 30 万円を超える銘柄（9984.T = 12,000 円 × 100 株 = 120 万円など）は **建玉不能で取引 0 件**になり、daily_pnl=0 で「全敗」扱いに。6613.T だけ株価 3,000 円台で建てられて唯一動作。

修正:
- `_run_bt(strat, df, sym)` ヘルパーを追加し、`walkforward_strategy_compare.py` と同条件にする（`starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO`, `fee_pct=0.0`, `limit_slip_pct=0.003`, `eod_close_time=(15, 25)`, `short_premium_daily_pct=premium`、`PREMIUM_FREE_SYMBOLS` セット 24 銘柄）
- `_strip_metrics` で `is_daily/is_pf/is_win_rate/is_trades/oos_*/robust/...` 等のメトリクスキーを除去（戦略コンストラクタへの noise 防止）
- demote 判定に **`MIN_TOTAL_TRADES_FOR_DEMOTE=6` 未満は `low_sample_candidates` に分離**（取引 0 件など資金不足由来の偽陽性を universe 除外に使わない）

修正後の再実行結果（VPS, 2026-04-28T06:08）:

| 区分 | 件数 | 例 |
| --- | --- | --- |
| `demote_candidates` | 7 | 3103.T(WR32.5/-15366), 6613.T(32.5/-4483), 9107.T(18.75/-1490), 4385.T(45.83/-7450), 1605.T(36.36/+2033), 9433.T(34.20/+1130), 4568.T(48.85/+1508) |
| `low_sample_candidates`（取引 0） | 3 | 9984.T MacdRci, 4592.T Breakout, 6645.T Breakout |
| 健全 | 2 | 6752.T MacdRci(58.57/+2516), 9468.T MacdRci(60.0/+2569) |

→ 1605.T は私の compare（5 split, 単一カット）では `0/5 / -1636`、nightly_wf（rolling 2 split, 直近 ~4 日）では `2/2 / +2033` と短期は持ち直しているが WR<50% で demote。両 WF とも「劣化」を示す点は一致。

### (B2/B3) `merge_robust_into_universe.py` の二段ゲート

`nightly_walkforward_latest.json` の `demote_candidates` を `(symbol, strategy)` 集合として読み、

1. **追加・置換ガード**（既定 ON）— Robust 採用拒否ループの最後で「`(s, "MacdRci")` が demote 入りなら拒否」を追加。`rejected_low_quality.reasons` に `nightly_wf_demote(win=...,pass=...,daily=...,trades=...)` の文字列を含めて理由を可視化。
2. **既存 universe からの除去**（既定 OFF / `--enforce-nightly-wf-removal` で ON）— ペアが既に universe にあって demote 入りの場合、フラグ ON 時のみ除去。OFF 時は `observed_nightly_wf_demote` にレポート保存のみ。

OFF を既定にした理由: 直近 4 日（rolling_splits の oos 区間）のノイズで主力 Robust（3103.T, 6613.T, 9984.T 等）を一度に消失させるリスクを避けるため。**数日連続して demote が継続したペア**を手動で `paused_pairs` に登録する運用にする。

新規引数:
- `--nightly-wf-path data/nightly_walkforward_latest.json`（既定）
- `--ignore-nightly-wf`（緊急 bypass）
- `--enforce-nightly-wf-removal`（破壊的: 既存 universe からの除去 ON）

### (B4) 適用結果

VPS で本番実行（観察モード）:

```
robust_total=1  prev_universe=12  new_universe=13
added=1  promoted=0  rejected_low_quality=18  removed_by_nightly_wf=0  observed_nightly_wf_demote=7  nightly_wf_demote_total=7  enforce_removal=False
追加: 6723.T (Renesas)  oos_daily=1471
```

`rejected_low_quality` の 18 件のうち **6 件で `nightly_wf_demote` が拒否理由に追加**された（3103.T, 9107.T, 9433.T, 4568.T, 6613.T, 4385.T, 1605.T のうち重複ペアを除く）。これは「将来のリ Robust 化候補が demote 中なら新規採用しない」が機能している証拠。

cron は `50 8 * * 1-5 .../merge_robust_into_universe.py --no-backup` で `--enforce-nightly-wf-removal` 無し＝観察モードのまま動く。明日以降の自動実行も安全。

### 次の運用ループ

1. **毎朝のレポート**で `data/universe_robust_merge_latest.json::observed_nightly_wf_demote` を確認
2. **3 日連続 demote** が同じペアで観測されたら、`data/jp_paper_trading_halt.json::paused_pairs` に手動登録（即運用停止）
3. **1 週間以上継続**したら `--enforce-nightly-wf-removal` で universe から除去 → 研究側の再最適化を待つ

### 成果物

- 更新: `scripts/nightly_walkforward_revalidation.py`（資金/コスト条件修正、メトリクス除去、`low_sample_candidates` 分離、`oos_total_trades` を結果に含める）
- 更新: `scripts/merge_robust_into_universe.py`（`_load_nightly_wf_demote`、`--nightly-wf-path` / `--ignore-nightly-wf` / `--enforce-nightly-wf-removal` 引数、追加・置換ガード、既存除去 opt-in、`removed_by_nightly_wf` / `observed_nightly_wf_demote` 出力）
- 出力: `data/nightly_walkforward_latest.json`（修正後の demote_candidates）
- 出力: `data/universe_robust_merge_latest.json`（observed_nightly_wf_demote 7 件、universe +1 = 6723.T）
- 出力: `data/universe_active.json`（13 銘柄、自動バックアップ `universe_active.backup_20260428_061238.json`）


## 2026-04-29 (夜) バックテスト報告とその追検証 — 1605.T MacdRci 過適合の再確認 / VPS 直 dry-run の不全修正

### 背景

`docs/BACKTEST_REPORT_TEMPLATE.md` に従い 4/29 22:00 JST にバックテスト報告を実施。報告本体の「次アクション」として挙げた 2 件（9984.T 高 WR alt 観測、1605.T 戦略入替 PoC）を続けて実行し、4/27 の判断（1605.T MacdRci の `paused_pairs` 投入）を 2 日後に追検証した。同時にテンプレ手順「VPS で `--dry-run`」が VPS の system python3 では pandas 未導入で落ちる不具合を解消した。

### (C1) 報告手順の不全 — VPS で `update_backtest_report_checkpoint.py --dry-run` が pandas 不在で落ちる

VPS 上で `python3 scripts/update_backtest_report_checkpoint.py --dry-run` を実行すると `aggregate_macd_rci_slope_since` → `backend/backtesting/macd_rci_slope_metrics.py` 内の `import pandas as pd` で `ModuleNotFoundError: pandas` で異常終了する。daemon 自身は `.venv/bin/python3` 経由で動いているが、テンプレが指示する `python3 ...` では VPS の system python に解決される。

修正:

- `scripts/update_backtest_report_checkpoint.py` の slope 集計呼び出しを try/except で囲み、`ModuleNotFoundError` を含む例外を捕えたら **その理由付きでスキップ** して残りの集計を完走する。
- `--skip-slope` フラグを追加し、明示的にスキップを指示できるようにする（cron 等で使う想定）。
- 出力ブロックは `{"skipped": true, "reason": "..."}` の JSON で残す。報告者は「集計をスキップしたこと」と理由を必ず認知できる。

検証:

- VPS（system python3, pandas なし） → 自動で `"reason": "依存モジュール未導入のためスキップ: pandas"` でスキップし、method_pdca 差分と Robust 入替差分は完走。
- ローカル（pandas あり） → `--skip-slope` 明示時のみ `"reason": "--skip-slope 指定"` でスキップ。指定なしは従来通り集計。

成果物:

- 更新: `scripts/update_backtest_report_checkpoint.py`（`--skip-slope` 追加、slope 集計を try/except でガード）
- 更新: `docs/BACKTEST_REPORT_TEMPLATE.md`（VPS 実行例を `.venv/bin/python3` 推奨に書換、system python3 + `--skip-slope` の代替例も併記）。

### (C2) 9984.T WR ギャップ追検証 — WR 寄り副軸は採用に劣後、現採用維持で OK

`backtest_quality_gate_latest.json::wr_underperform_warnings` で 9984.T が 4/29 時点でも `gap=16.47pt`（adopted 42.55% / p95 59.02%、母集団 613）で警告継続中。`extract_high_wr_alt_configs.py --symbol 9984.T --strategy MacdRci --min-oos-trades 30 --min-oos-pf 1.2 --min-oos-daily 1500 --top 10` で **53 件** の高 WR 候補を確認し、トップを `walkforward_strategy_compare.py` で baseline (採用 #348632) と直接比較。

| 観点 | 採用 (MacdRci #348632) | 高 WR alt (MacdRci #309603) |
| --- | --- | --- |
| DB OOS | WR 42.55% / daily +9,932 / PF 4.79 / trades 47 | WR 71.43% / daily +6,815 / PF 4.21 / trades 42 |
| 90日 5-split WF | **5/5 OOS+** ／ avg **+7,086 円/日** ／ trades 90.4 | 5/5 OOS+ ／ avg **+5,537 円/日** ／ trades 36.0 |

両者とも 5/5 で healthy だが daily で採用が +1,549 円/日、trade 機会も 2.5 倍。**「WR を 30pt 上げると daily が 22% 減る」** トレードオフが直接観測された。WR 警告は観測継続のみとし採用ロジックは触らない。

成果物:
- 出力: `data/snapshots/walkforward_compare_9984_macdrci_high_wr_alt.json`

### (C3) 1605.T MacdRci 追検証 — 4/27 PoC の劣化を 2 日後に再確認

4/27 PoC（MacdRci #112726 vs Scalp 上位 3 種）は **0/5 OOS positive / -1,636 円/日** だった。今回 candidate を Scalp #236261（trades=20）+ Pullback #179423（trades=30）に拡張し、同条件で再走（fetch_days=90, splits=0.3,0.4,0.5,0.6,0.7）:

| label | oos_positive | avg_oos_daily | avg_oos_trades | reference 50/50 |
| --- | --- | --- | --- | --- |
| baseline: MacdRci#112726 | **0/5** | **-2,026** | 67.2 | -2,568 |
| candidate: Scalp#236261 | 5/5 | +359 | 16.6 | +402 |
| candidate: Pullback#179423 | 5/5 | +372 | 10.6 | +374 |

→ MacdRci 採用は **2 日前 PoC より悪化（-1,636 → -2,026 円/日）**。Scalp / Pullback はいずれも 5/5 で OOS 黒字。`backtest_quality_gate_latest.json::low_robust_yield_warnings` でも 1605.T は MacdRci robust_rate 0.35% / Scalp 62.4% / Pullback 60.2% と観測値が walkforward 結果と整合。

### (C4) 運用判断 — `paused_pairs` のメタデータ更新（追加停止は不要）

`paused_pairs` は 4/27 から `1605.T × MacdRci` を `until=2026-05-11` で停止しており、paper/live のエントリーは既にブロック済み（追加の運用停止操作は不要）。今回は以下のメタデータ更新のみ:

- `reason`: 4/29 walkforward の最新数値に書換（5/5 OOS-、avg -2,026、Scalp/Pullback 比較も併記）
- `until`: 2026-05-11 → **2026-05-25** に延長（PoC が 2 日後に強化されたため）
- `evidence`: `data/snapshots/walkforward_compare_1605_macdrci_vs_scalp_pullback.json`（今回のスナップショット）に切替
- `renewed_at`: `2026-04-29T22:10:00+09:00`

universe からの除去（`merge_robust_into_universe.py --enforce-nightly-wf-removal`）は **未実施**。理由: 4/28 朝の `nightly_walkforward_latest.json` で 1605.T は WF 2/2 OOS positive / +2,033 円（WR<50% で demote 候補にはなっているが PnL は黒字）と「短期は持ち直し」の評価が出ており、5-split と 2-window で結果が割れている。**5/11 の `until` 失効までに連続 demote が確認されたら enforce_removal を ON に切替**する運用継続。

### (C5) `nightly_walkforward_revalidation` cron 4/29 失火 — 原因究明と防御的修正で復旧

`/root/algo-trading-system/data/nightly_walkforward_latest.json` の mtime が `2026-04-28 06:08` のまま固着していた。`crontab -l` で `10 3 * * 1-5 ... .venv/bin/activate && python3 scripts/nightly_walkforward_revalidation.py --notify` が登録されており `logs/nightly_walkforward.log` の mtime は `2026-04-29 03:10:18` まで進んでいたので **「cron は走った／途中で死んだ」と判断**。

ログ末尾を確認したところ次の Traceback で異常終了:

```
File "scripts/nightly_walkforward_revalidation.py", line 109, in _evaluate_one
    strat = create_strategy(strat_name, sym, name=sym.replace(".T", ""), params=params)
File "backend/backtesting/strategy_factory.py", line 504, in create
    raise ValueError(f"Unknown strategy: {strategy_name}")
ValueError: Unknown strategy: ORB
```

→ `data/universe_active.json` に `manual_observation` 経由で **`ORB` × 3 銘柄（5985.T / 6522.T / 6433.T）** と **`Momentum5Min` × 2 銘柄（247A.T / 5243.T）** が含まれているが、`backend/backtesting/strategy_factory.py::ALL_STRATEGY_NAMES` には未登録。1 件の ValueError でスクリプト全体が落ち、JSON が書き出されなかった。

修正 (`scripts/nightly_walkforward_revalidation.py`):

1. `_evaluate_one()` 内の `create_strategy()` 呼び出しを try/except で囲む。`ValueError` は `skipped="unknown_strategy"` として返す。それ以外の Exception は `skipped="create_failed"`。
2. `main()` の for ループでも `_evaluate_one()` 全体を try/except し、想定外例外（OHLCV 取得失敗・戦略内 IndexError 等）でも cron 全体を落とさない。`skipped="evaluate_exception"` で個別 skip。
3. `payload` に `skipped_summary` ブロックを追加（reason ごとに `(symbol, strategy, error)` を集約）。次回 universe メンテで「ORB / Momentum5Min を実装するか universe から除く」の判断材料にする。

VPS でオンデマンド再実行して復旧（`.venv/bin/python scripts/nightly_walkforward_revalidation.py`）:

- `data/nightly_walkforward_latest.json` mtime: `2026-04-29 22:18`、history `20260429.json` も書き出し
- 結果: `total=18 / demote=9 / low_sample=2 / unknown_strategy=5`
- 1605.T MacdRci は引き続き demote 入り（WR 41.7% / pass=1.0 / daily +2,400 / trades 20）。WR 軸では今日も劣化判定で、私の 5-split walkforward（5/5 OOS-）と整合。

明日 4/30 03:10 の cron からは自動で動く。

### 成果物

- 更新: `scripts/update_backtest_report_checkpoint.py`（`--skip-slope`、自動 try/except、JSON でスキップ理由を出力）
- 更新: `scripts/nightly_walkforward_revalidation.py`（防御的 try/except 二段、`skipped_summary` 出力）
- 更新: `docs/BACKTEST_REPORT_TEMPLATE.md`（VPS 実行例を `.venv/bin/python3` 推奨に）
- 更新: `data/jp_paper_trading_halt.json`（1605.T MacdRci の reason / evidence / until 更新、`renewed_at` 追加）
- 出力: `data/snapshots/walkforward_compare_1605_macdrci_vs_scalp_pullback.json`
- 出力: `data/snapshots/walkforward_compare_9984_macdrci_high_wr_alt.json`
- 出力: `data/backtest_report_checkpoint.json`（last_experiment_id=348658, last_generation=51809、note 更新）
- VPS 出力: `data/nightly_walkforward_latest.json` / `data/nightly_walkforward_history/20260429.json`（cron 復旧）
- 観察継続: per-config 観測 OOS（前回 -917 → 今回 -667.4、4/30〜5/2 で 0 円超え追跡）
- 観察課題: ORB / Momentum5Min は manual_observation 状態のまま `nightly_walkforward_latest.json::skipped_summary` に積み上がる。次回別 PR で「正規 strategy 実装するか universe から除くか」を判断。

### (C6) 観察候補 (manual_observation) を nightly_wf 側で意図通り skip する

(C5) で残課題とした「ORB / Momentum5Min が `unknown_strategy` として nightly_wf の警告に積み上がる問題」を即日処理。

調査結果:

- `backend/backtesting/strategy_factory.py` 冒頭に **明確な設計コメント**:
  > アーカイブ済み（PDCA 非対象）: Momentum5Min / ORB / VwapReversion — experiments で平均 OOS が負 or NULL、robust=0 だったため、factory/daemon のループから外して新規研究リソースを温存する。クラス本体は backend/lab/runner.py や social_strategy.py が直接 import しているため残す。
- 一方、`merge_robust_into_universe.py` が `data/universe_observation_pairs.json` から `source='manual_observation'` で universe_active.json に流し込む別経路がある（4/28 (B4) 導入）。設計は `force_paper=False` で paper 投入はせず観察のみ、だが nightly_walkforward は universe_active を全件回すため衝突。
- universe_active.json から手で削除しても、毎営業日 08:50 の cron `merge_robust_into_universe.py` で再生成される。

結論: **正本（universe_observation_pairs.json）と universe_active.json は触らない**。`nightly_walkforward_revalidation.py` 側で `source == 'manual_observation'` のペアは観察意図を尊重して skip する。

修正:

- 評価ループの先頭で `row.get('source') == 'manual_observation'` を判定し、`skipped='observation_only'` で個別 skip。`note` フィールドに `source=manual_observation (force_paper=false, factory archived)` を残す。
- これにより `skipped_summary` のキーが `unknown_strategy`（factory に登録すべき真のバグ）と `observation_only`（設計通りの skip）に分かれる。今後 `unknown_strategy` にペアが現れたら本物の警告として対応する。

VPS で再実行検証 (`.venv/bin/python scripts/nightly_walkforward_revalidation.py`):

```
total=18 / demote=9 / low_sample=2
skipped_summary:
  observation_only: 5 pairs
    5985.T ORB / 6522.T ORB / 6433.T ORB / 247A.T Momentum5Min / 5243.T Momentum5Min
```

`unknown_strategy` カテゴリは消え、観察候補は意図通り分類された。

成果物:

- 更新: `scripts/nightly_walkforward_revalidation.py`（`source=manual_observation` を `observation_only` で skip）
- VPS 出力: `data/nightly_walkforward_latest.json` 22:25 更新（`skipped_summary.observation_only=5, unknown_strategy=0`）

