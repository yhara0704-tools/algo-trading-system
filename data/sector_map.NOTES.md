# data/sector_map.json メモ

東証業種を主軸とするセクターマップ。物色テーマ(レアアース/パワー半導体/化粧品 等)は `data/theme_map.json` で別管理する二軸運用。本ファイルでは sector_strength の安定性を優先し、業種混在テーマセクター(フィジカルAI/小売IT/建設サービス/光半導体)は本メモで明示する。

新規銘柄追加時は `scripts/lookup_kabutan.py` で業種・テーマを取得し、業種は `data/sector_map.json`、テーマは `data/theme_map.json` に追加する。

## 注意 (2026-04-30 復旧時記録)

- かつて `data/sector_map.json` のトップレベルに `_doc` というメタ文字列キーで本注記が同梱されていたが、`backend/backtesting/trade_guard.py:get_sector` が「全 value は `{"domestic": [...]}` 形式」を前提に `info.get("domestic", [])` を呼ぶため、`AttributeError: 'str' object has no attribute 'get'` で `backtest-daemon.service` がクラッシュループした実績あり (約13時間)。
- 対策として:
  1. JSON 本体からは `_*` で始まるメタキーを排除し、本ファイル (`sector_map.NOTES.md`) に切り出す。
  2. ローダー側 (`backend/backtesting/trade_guard.py:_load_sector_map`) でも `key.startswith("_")` または `not isinstance(value, dict)` の要素を skip する防御を追加。
- 二重防御により、将来同型のメタコメントが誤って混入しても daemon は落ちない。
