# バックテスト報告テンプレート（Cursor向け）

ユーザーから「バックテスト報告して」と言われたら、**必ずこのテンプレに沿って**報告すること。

**報告の目的は「事実の一覧」ではない。** 前回以降どんな意図・仮説で探索したか、何が言えて何が言えないか、**だから次に何をする／しないか**まで含めた **意思決定メモ**として書く。数値と表はその **根拠** であり、本文の主役ではない。

箇条書きダラダラはNG。**結論（先頭）**・**前回からの差分**・**根拠となる表・数値**・**次アクション（最大3件）**をセットで出す。

**スナップショットだけの報告は不可。** 正本の「いまの全体像」は差分とセットで初めて意味がある。

---

## 用語（このテンプレの表記）

| 用語 | 意味 |
|------|------|
| **IS日次** | In-sample 区間での 1 営業日あたり損益（円/日イメージ）。`is_daily` 等 |
| **OOS日次** | Out-of-sample 区間での 1 営業日あたり損益。`oos_daily` 等。パラメータ選定に使った区間とは別の検証側 |

---

## 最重要: 正しい参照ファイルを使う

本システムには **`backtest_daemon.py`** が常駐し、成果が以下に逐次書き出される。**これが「いまのバックテスト結果」の正本**（VPS 上のパスは運用に合わせる）。

| ファイル | 中身 | 更新頻度 |
|----------|------|----------|
| **`data/macd_rci_params.json`** | MACD×RCI 銘柄別最適パラメータ（勝率・`last_updated` 等） | 銘柄ごとにグリッド完了のたび |
| **`data/strategy_fit_map.json`** | 銘柄×手法の適性（`best_strategy` / `best_robust`）、`updated` | デーモンの `research_canonical_sync` が Robust ベストをマージしたとき（および手動ワンショット更新時） |
| **`data/daemon_state.json`** | 進捗・クイックスキャン `qs_results` 等 | 常時 |
| **`data/strategy_knowledge.json`** | 蓄積学習（サイズ大） | 常時 |
| **`data/backtest_data_quality_latest.json`** | OHLCV品質チェック（欠損/異常値/除外率） | 世代更新時 |
| **`data/backtest_quality_gate_latest.json`** | バックテスト品質ゲート（DD/安定性/コスト差分、および per-config 観測平均・`top_repeat_clusters`） | 日次/任意 |
| **`data/paper_low_sample_excluded_latest.json`** | 推し運用（paper/live）でサンプル不足除外された銘柄と、WF 緩和が効いた銘柄の記録 | `collect_universe_specs` 呼び出しのたび |

### やってはいけないこと

- **`scan_full_pool_result.json` / `scan_multi_strategy_result.json` / `gate_comparison_result.json` を「最新」として単独報告すること**  
  手動ワンショットの過去スナップが多い。デーモン正本との**比較用**に留める。
- **mtime を確認せず「最新」と書くこと** — `ls -la --time-style=full-iso` で鮮度を確認する。

---

## 報告手順（この順番）

### Step 1: デーモン生存と鮮度

```bash
ssh bullvps "ps aux | grep backtest_daemon | grep -v grep"
ssh bullvps "ls -la --time-style=full-iso /root/algo-trading-system/data/{macd_rci_params,strategy_fit_map,daemon_state}.json"
# daemon は systemd 管理（backtest-daemon.service, StandardOutput=journal）
ssh bullvps "journalctl -u backtest-daemon.service --since '30 minutes ago' --no-pager | grep -E 'Plan|cluster_share|cluster_cooling|Generation' | tail -30"
# 旧 FileHandler 出力（参考）
ssh bullvps "tail -30 /tmp/daemon_detail.log"
```

- デーモン停止なら冒頭に **🚨 デーモン停止**
- 正本の mtime が 24h+ 動いていなければ **進捗停滞** と明記
- systemd drop-in で有効化した env（例: `BACKTEST_CLUSTER_COOLING_ENABLED=1`）は `systemctl show backtest-daemon.service -p Environment` で確認する。

### Step 2: 差分（必須）— チェックポイント

報告のたびに **前回の報告境界** と突き合わせる。境界は `data/backtest_report_checkpoint.json` の `last_experiment_id`（および `last_reported_at`）。

**VPS 上の DB が正の環境なら、VPS で実行:**

```bash
# 推奨: daemon と同じ .venv で実行する（system python3 だと pandas 等が無いことがある）
ssh bullvps "cd /root/algo-trading-system && .venv/bin/python3 scripts/update_backtest_report_checkpoint.py --dry-run"

# system python3 で pandas 等が無い場合は --skip-slope を使うか、未指定でも自動で
# slope 集計だけスキップして他の差分は完走する（2026-04-29 修正）。
ssh bullvps "cd /root/algo-trading-system && python3 scripts/update_backtest_report_checkpoint.py --dry-run --skip-slope"
```

ローカル DB が VPS と同期している場合のみローカルでも可。

**ローカルで数値を出す前に正本を揃える（推奨）:** VPS から `data/algo_trading.db`・各種 JSON を `scp` で取得する。

```bash
cd /path/to/algo-trading-system && .venv/bin/python scripts/sync_canonical_from_vps.py
```

チェックポイントの `--dry-run` / 保存と合わせて同期する場合:

```bash
.venv/bin/python scripts/update_backtest_report_checkpoint.py --sync-from-vps --dry-run
```

（`--sync-from-vps` は実行の先頭で同期し、その後に集計する。報告単位でチェックポイントを進めたあと、ローカル検証用にもう一度 `sync_canonical_from_vps.py` を回すとよい。）

- **機械集計**: `--dry-run` 出力の「前回チェックポイント以降の `method_pdca`」ブロックを報告に含める（`trials` / `robust_count` / `robust_rate` 等）。
- **反復集中とクラスタ冷却（Phase F6）**: `data/backtest_quality_gate_latest.json` の `top_repeat_clusters` を読み、前回報告以降の **順位変動**（1 位に居続ける (strategy, symbol) / 新しく入った / 落ちた）と **share の推移**（15% を超えたままか、割り込んだか）を書く。journal の `cluster_share[Nd] top:` 行は生値のダブルチェック用。`cluster_cooling: N クラスタを冷却 (robust A → B)` が Generation ごとに出ていれば冷却が実運用で効いている。冷却が ON か OFF かも明記する（`systemctl show backtest-daemon.service -p Environment` で確認可能）。
- **per-config 観測 OOS**: `backtest_quality_gate_latest.json` の `avg_oos_daily_pnl`（単純平均）と `avg_oos_daily_pnl_by_config`（(strategy, symbol, params_json) ごとの外側平均）の差を見る。両者が近づいていれば反復集中が解けた兆候。per-config が単純平均より大きく低いうちは **ポジティブ側に反復集中** の疑いを持ち続ける。
- **推し運用除外の増減**: `data/paper_low_sample_excluded_latest.json` の `excluded_count` / `wf_relaxed_hits` を前回と比較する。`wf_relaxed_hits` は WF 再現性の裏付けがついてサンプル不足でも推し運用に昇格した銘柄（現時点では 0 件運用が標準）。
- **MacdRci OOS 傾き（RCI/GC）**: 同じ `--dry-run` 出力の「MacdRci OOS 傾き集計（`rci_slope_summary_json`）」ブロックを参照する。各実験の OOS シグナルから算出したサマリーが DB に蓄積され、`aggregate_macd_rci_slope_since(last_experiment_id)` でチェックポイント境界の集計が取れる。
  - **読み方の目安**: `parsed_summaries`（パースできたサマリー件数）と `rows_with_json`（DB 上の非空行数）がゼロなら、当該差分期間に傾き JSON がまだ無い。`experiment_count` が集約に入った実験数。`rci_entry_mode_histogram` はエントリー方式（0=RCI 多数決、1=GC/DC・翌足、2=当足）の件数分布。`avg_rci_short_slope_oos_mean` や `avg_slope_at_long_signal_oos_mean` はチェックポイント境界内の単純平均（目安）。`avg_oos_gc_bar_count` は OOS で GC バーが立った行の平均件数。
- **正本の人間向け差分**: 前回報告以降に変わったことを必ず書く（最低限の例）:
  - **Robust 銘柄の増減**（新規に `robust: true` になった銘柄 / 外れた銘柄）
  - **`last_updated` が進んだ銘柄**（グリッド・再探索の進み）
  - **`daemon_state` の進捗数値の差**（例: `done_grid_search` の件数、generation）

**初回だけ例外**: チェックポイントが未設定（`last_experiment_id` が null）のときは、見出し **「前回からの差分」** に  
`初回報告のためチェックポイント未設定。method_pdca 以降の機械差分はスクリプト出力の通り（全期間に近い）。本報告後に必ずチェックポイントを保存する。`  
と書く。**2回目以降はこの省略をしない。**

**報告が一段落したらチェックポイントを進める（必須）:**

```bash
# 推奨: daemon と同じ .venv で実行
ssh bullvps "cd /root/algo-trading-system && .venv/bin/python3 scripts/update_backtest_report_checkpoint.py --note \"（報告単位のメモ）\""
```

- 差分の内部実装: `get_method_pdca_aggregate_since(last_experiment_id)` / MacdRci 傾きは `aggregate_macd_rci_slope_since(last_experiment_id)`
- 週次の補助: `scripts/snapshot_method_pdca_weekly.py` → `data/snapshots/method_pdca_weekly_YYYY-MM-DD.json`（任意だが差分の説明に使える）
- **銘柄集中度（method_pdca）**: `scripts/report_method_pdca_symbol_concentration.py`（既定でチェックポイント `last_experiment_id` より後の symbol 別件数）。週次 cron は `scripts/setup_research_ops_cron.py` が `logs/method_pdca_symbol_concentration.log` に出力
- 週次の補助（品質）: `scripts/snapshot_backtest_quality_weekly.py` → `data/snapshots/backtest_quality_weekly_YYYY-MM-DD.json`
- **擬似ポートフォリオ日次（3 定義）**: `backtest_daily_agg` を `date` で `SUM` する。デーモンがポートフォリオSim実行時に `strategy_id=portfolio_latest_curve` として暦日別を反映する。確認は `scripts/print_pseudo_portfolio_stats.py`。

### Step 3: 正本の取得

```bash
ssh bullvps "cat /root/algo-trading-system/data/macd_rci_params.json"
ssh bullvps "cat /root/algo-trading-system/data/strategy_fit_map.json"
ssh bullvps "cat /root/algo-trading-system/data/daemon_state.json"
```

### Step 4: 下記フォーマットで報告本体を出力

**本文の順序は必ず次のとおり。** ダッシュボード用の **日付付きタイトル行**（例: `【BT daemon】2026-04-17 …`）や、数値だけのワンライナー見出しは **書かない**。

---

## 報告フォーマット本体

### 1. 結論・意思決定（必ず最初・これが報告の中心）

前回報告以降、**どんな意識・優先度でバックテスト／探索を回したか**（例: 銘柄を絞った、手法PDCAを厚くした、など）を一文で置く。

続けて、箇条書きでも短い段落でもよいが、必ず含める:

- **わかったこと**（解釈。数値への当てはまりを一言で）
- **まだわからないこと／限界**（サンプル不足、探索の偏り、外生要因、再現性未検証など）
- **だから次にこうする／こうしない**（または「こうするか検討したい」と明示）。曖昧ならその理由も

ここまでが「報告」の本体。**ここが空なら報告未完了**とみなす。

### 2. 前回からの差分（必須）

| 区分 | 書く内容 |
|------|----------|
| **DB（method_pdca）** | `--dry-run` の集計（試行数・Robust 件・率など） |
| **正本（JSON）** | Robust の増減、`last_updated` の更新があった銘柄、進捗カウンタの変化 |
| **品質（JSON）** | `backtest_data_quality_latest` の issue_rate、`backtest_quality_gate_latest` の合否、`avg_oos_daily_pnl` と `avg_oos_daily_pnl_by_config` の乖離 |
| **反復集中（Phase F6）** | `top_repeat_clusters` の 1 位と share、閾値（15%）超のクラスタ数、journal の `cluster_cooling` 出現有無。冷却 ON/OFF（`systemctl show backtest-daemon.service -p Environment`）も明記 |
| **推し運用除外** | `paper_low_sample_excluded_latest.json` の `excluded_count` / `wf_relaxed_hits`（WF 緩和で救われた銘柄） |

前回から期間が空いていても、**「変化なし」ならその一行**（例: 「Robust 銘柄集合に変化なし。generation +N のみ」）を必ず書く。反復集中と冷却は **「変化なし」でも 1 行**（例: 「3103.T クラスタが引き続き 1 位・share 30%、冷却継続」）を残す。

### 3. バックテストの深掘り（事実の羅列ではなく「検討した内容」）

表の前に、必要に応じて **短く** 書く（該当しなければ「特になし」でよい）:

- 仮説と結果のズレ、**多重検定・探索バイアス**への当たり方
- **レジーム・流動性・トレード回数**で読みが変わる点
- スプリットや感度で **ブレる／ブレない** の見立て
- ログや `experiments` の偏り（特定銘柄に試行が集中していないか等）
- **反復集中の副作用**: `top_repeat_clusters` 1 位が期間の大半を支配していると、単純平均 OOS は見かけ以上にその銘柄で引っ張られる。per-config 平均との差を踏まえて読む。冷却 ON 中は「冷却されている銘柄の OOS は当期間では更新されにくい」ことも留意。

### 4. Robust（`macd_rci_params.json` で `robust: true`）— 根拠表

- **件数**は一行（例: 「Robust 計 N 銘柄」）。**表は上位 3 件のみ**（並び基準を1つに固定）。
- **4 位以下は表に出さない**。特記事項がある銘柄だけ表外に **短いメモ**。

| 順位 | 銘柄 | IS日次 | IS PF | IS勝率 | OOS日次 | OOS PF | OOS勝率 | パラメータ要約 | 更新日 |
|------|------|--------|-------|--------|---------|--------|---------|----------------|--------|

（データ行は **3 行まで**。）

### 5. 準 Robust / NG 典型 / 適性マップ

- **準 Robust**: 必要なときだけ要約
- **NG 典型**: 最悪 3 件まで＋全体件数のサマリーのみ
- **適性マップ**: 分布の要点（例: MacdRci best が X 銘柄、など）

### 表記ルール

- 勝率・PF が JSON にあるときは **損益と必ず併記**
- 勝率が欠ける行は **「PF のみ（勝敗内訳なし）」** と書く

### 判定の凡例

- **Robust**: `robust: true` の定義に従う
- **IS-only** / **NG** / **サンプル不足**（`is_trades` / `oos_trades` が小さい）は **過信しない** と明記

### 次アクション（3 件以内）

「結論」で触れた方針を、**実行可能な手**に落とす（例）:

1. 上位 Robust の **フォワード／スプリット再現**（`scripts/walkforward_robust_macd_rci.py` 等）
2. **正本と DB の整合**（`is_trades` / `oos_trades`、canonical 同期）
3. **method_pdca** のレビューと、枠の見直し・打ち切り

---

## 再掲: 報告で避けること

- ❌ **結論・意思決定なし**で、メタと表だけ並べて終わること
- ❌ **ニュース風のタイトル行**（日付・件数だけのワンライナー）で代用すること
- ❌ **前回からの差分なし**（初回の例外以外。チェックポイント未更新のまま「全体だけ」を繰り返すこと）
- ❌ `scan_*_result.json` だけを「今週の正」とする
- ❌ JSON の**生貼り全文**
- ❌ 損益だけ（勝率・PF なし）
- ❌ 感想のみ（根拠なき印象）

## ユーザーの基本原則

- **まず負けない** — OOS が悪い候補を推し運用にしない
- PF < 1.0 は推奨しない
- トレード数が少ない結果は **サンプル不足**として扱う

---

## 固定テンプレ（最終出力ひな形）

以下をそのまま使って埋める。  
**順序は固定**、見出し名も原則このまま。余計なタイトル行（`【BT daemon】...` 形式）は付けない。

### 1) 結論・意思決定

- 今回どんな意図・優先度でバックテストを回したか:
- わかったこと:
- まだわからないこと／限界:
- だから次にこうする／こうしない:

### 2) 前回からの差分（必須）

- DB（method_pdca, checkpoint 以降）: `trials=`, `robust_count=`, `robust_rate=`
- 正本（JSON）:
  - Robust 銘柄の増減:
  - `last_updated` が進んだ銘柄:
  - 進捗カウンタ変化（例: `done_grid_search`, generation）:
- 反復集中と冷却（Phase F6）:
  - `top_repeat_clusters` 1 位: `(strategy, symbol) share=X%`（前回比 `+/- Y pt`）
  - 15% 超クラスタ数: `N`（前回比 `+/- M`）
  - 冷却 ON/OFF: `ENABLED=...`、直近 Generation で `cluster_cooling: N クラスタを冷却` が出ているか
- per-config 観測 OOS:
  - `avg_oos_daily_pnl` / `avg_oos_daily_pnl_by_config` の差が縮んだ/広がった
- 推し運用除外（paper/live）:
  - `paper_low_sample_excluded_latest.json`: `excluded_count=`、`wf_relaxed_hits=`
- 差分がない場合: `変化なし（理由: ...）` を1行で明記

### 3) バックテストの深掘り

- 仮説と結果のズレ（あれば）:
- 多重検定・探索バイアスの見立て:
- レジーム/流動性/トレード回数による読み替え:
- 再現性（スプリット・感度）の評価:
- 反復集中の副作用（冷却対象銘柄の OOS 更新度合い、per-config 平均との乖離）:

### 4) Robust（上位3のみ）

- Robust 計 `N` 銘柄

| 順位 | 銘柄 | IS日次 | IS PF | IS勝率 | OOS日次 | OOS PF | OOS勝率 | パラメータ要約 | 更新日 |
|------|------|--------|-------|--------|---------|--------|---------|----------------|--------|
| 1 | | | | | | | | | |
| 2 | | | | | | | | | |
| 3 | | | | | | | | | |

- 4位以下で特記事項がある銘柄（任意）:

### 5) 次アクション（最大3件）

1.
2.
3.

### 6) 実施確認（報告内で完了した運用作業）

- checkpoint dry-run 実行: Yes / No
- checkpoint 更新実行: Yes / No
- （必要なら）上位銘柄の再現性テスト実行: Yes / No
- 冷却 ON/OFF の変更または維持の理由（`BACKTEST_CLUSTER_COOLING_ENABLED`）: ON のまま / OFF のまま / 切替（理由）
