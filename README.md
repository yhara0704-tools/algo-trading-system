# Algo Trading Terminal

リアルタイムトレーディングターミナル — FastAPI + WebSocket + Chart.js

**ターミナル風黒緑UI** でCoinbase/Polymarketのスプレッド分析を含む多アセットリアルタイム監視。

## 機能

| 機能 | 詳細 |
|---|---|
| **Coinbase WS** | BTC/ETH/SOL/XRP リアルタイム価格 (公式WebSocket) |
| **Polymarket** | BTC 5分予測市場の確率→インプライドプライス取得 |
| **スプレッド分析** | Coinbase Spot vs Polymarket Implied の乖離検出・売買シグナル |
| **FX** | USD/JPY, EUR/USD, GBP/USD, EUR/JPY, AUD/USD |
| **先物** | S&P500, Nasdaq100, ダウ, 金, 原油, 日経225先物 |
| **日本株** | トヨタ, ソニー, SoftBank, キーエンス, MUFG, 信越化学 |
| **米国株** | Apple, Microsoft, NVIDIA |
| **チャート** | Chart.js OHLCV + ボリューム (1m/5m/15m/1h/1d) |

## 起動

```bash
./run.sh
```

ブラウザで http://localhost:8000 を開く。

## スプレッド分析ロジック

```
spread_pct = (coinbase_spot - polymarket_implied) / coinbase_spot × 100

spread > +0.3%  → SHORT シグナル (市場がspot下落を予測)
spread < -0.3%  → LONG  シグナル (市場がspot上昇を予測)
|spread| < 0.3% → NEUTRAL
```

信頼度は乖離幅に応じて0〜100%で計算。

## アーキテクチャ

```
frontend/
  index.html          ターミナルUI (HTML/CSS/JS)
  static/css/         黒緑CRTスタイル
  static/js/app.js    WebSocket + Chart.js クライアント

backend/
  main.py             FastAPI + WebSocket hub
  ws_manager.py       WebSocket接続管理
  feeds/
    coinbase_feed.py  Coinbase Advanced Trade WS
    polymarket_feed.py Polymarket Gamma API polling
    multi_asset_feed.py yfinance FX/先物/株式
  analysis/
    spread_analyzer.py スプレッド計算・シグナル生成
  routers/
    api.py            REST API (/api/assets, /api/ohlcv, /api/spread, ...)
```

## API エンドポイント

| エンドポイント | 説明 |
|---|---|
| `GET /api/assets` | 全アセット一覧+最新価格 |
| `GET /api/price/{symbol}` | 個別シンボル価格 |
| `GET /api/ohlcv/{symbol}` | OHLCVローソク足 |
| `GET /api/spread` | Coinbase/Polymarketスプレッド |
| `GET /api/polymarket/markets` | アクティブBTC予測市場一覧 |
| `WS /ws` | リアルタイム価格配信 |

## 注意事項

- yfinance は遅延データ (60秒ポーリング)
- Coinbase WebSocket は認証不要でリアルタイム
- Polymarket は30秒ポーリング
- 本ツールは情報提供のみ。投資判断には使用しないこと
