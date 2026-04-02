# バックテスト検証スケジュール

更新: 2026-04-01

## 検証ブロック一覧

| ブロック | 時間帯 | 内容 | 状態 |
|---------|--------|------|------|
| A | 今夜 18:00〜 | MACD×RCI 新規銘柄IS/OOS（未評価30銘柄） | 実行中 |
| B | 今夜 18:00〜（並行） | 6手法 × ターゲット4銘柄 横断比較 | 実行中 |
| C | 深夜〜翌朝 | 全58銘柄 × MACD×RCI 一括スキャン（価格フィルター付き） | 実行中 |
| D | 翌朝〜 | ブロックA〜Cの結果集計→次回検証候補リスト作成 | 待機 |
| 毎週月曜8:30 | 週次 | 週次最適化cron（IS/OOS+相場適性スコア+VPS転送） | cron済み |

## 各ブロック詳細

### Block A: MACD×RCI 新規銘柄
対象: 価格フィルター（≤3,267円）を満たす未評価銘柄 最大30銘柄
手法: IS/OOS グリッドサーチ（60日 前半30IS/後半30OOS）
判定: Robust(IS+OOS両方プラス) / IS-only / NG
出力: scan_macd_rci_new.log, scan_macd_rci_new_result.json

### Block B: 6手法横断比較
対象: M3(2413), Sony(6758), Sanrio(8136), Unitika(3103), QDレーザ(6613)
手法: MacdRci / Breakout / Scalp / Momentum5Min / ORB / VwapReversion
IS: 30日, OOS: 15日（クイックスキャン）
出力: scan_multi_strategy.log, scan_multi_strategy_result.json

### Block C: 全58銘柄 × MACD×RCI 一括スキャン
対象: PTS_CANDIDATE_POOL全銘柄（価格フィルター後）
手法: MACD×RCI デフォルトパラメータのみ（グリッドなし、IS/OOSスコアだけ）
目的: 銘柄適性マップ作成 — どの銘柄がMACD×RCIと相性がいいか
出力: scan_full_pool.log, scan_full_pool_result.json

## 次回候補リスト（Block D で自動更新）
Block A/B/Cの結果から:
- Robust率 > 0 または IS PF > 1.2 の銘柄を優先候補に昇格
- NG × 複数手法 の銘柄は「適性低」フラグ
- 手法別 Top5 を next_targets.json に保存

## 過去の検証結果サマリー（2026-04-01）

### MACD×RCI IS/OOS グリッドサーチ
| 銘柄 | 判定 | IS日次 | OOS日次 | pyramid |
|------|------|--------|---------|---------|
| M3 (2413.T) | **Robust** | +1,131円 | +246円 | 2 |
| Sony (6758.T) | IS-only | +603円 | - | 0 |
| Sanrio (8136.T) | IS-only | +544円 | -445円 | 0 |
| Unitika (3103.T) | NG | -2,702円 | -1,645円 | 0 |
