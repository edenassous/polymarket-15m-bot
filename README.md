# Polymarket Trading Bots

Automated trading bots for [Polymarket](https://polymarket.com) prediction markets, focused on BTC Up/Down markets and various arbitrage strategies.

## Strategies

### Live Trading Bots

| Bot | File | Description |
|-----|------|-------------|
| **15m K-Means** | `polymarket/kmeans_15m_trader_v1_live.py` | Live trader for 15-minute BTC Up/Down markets using K-means S/R clustering, intra-window momentum, and volume confirmation. Web dashboard on port 5055. |
| **Hourly K-Means** | `polymarket/kmeans_hourly_trader_v2_live.py` | Live trader for hourly BTC Up/Down markets with K-means clustering, dynamic stake sizing, and trend filters. Web dashboard on port 5054. |


## How It Works

The main live trading bots (15m and hourly) use a K-means clustering approach:

1. **Fetch BTC candles** from Binance via CCXT
2. **Cluster price levels** using K-means to identify dynamic support/resistance zones
3. **Generate signals** based on intra-window BTC momentum (price vs window open)
4. **Filter trades** using ATR volatility, volume ratio, market trend confirmation, and consensus sampling
5. **Place orders** on Polymarket's CLOB via `py-clob-client` with slippage protection
6. **Track P&L** using actual fill prices (not displayed market prices)
7. **Auto-resolve** trades when markets close, recording win/loss in SQLite

Each bot includes a built-in Flask web dashboard for monitoring trades, signals, and P&L in real time.

## Setup

### Prerequisites

- Python 3.9+
- A [Polymarket](https://polymarket.com) account with a funded wallet
- MetaMask or compatible wallet for the private key

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/trading-scripts.git
cd trading-scripts
pip install -r requirements.txt
```

### Configuration

1. Copy the example environment file and fill in your credentials:

```bash
cp polymarket/.env.example polymarket/.env
```

2. Edit `polymarket/.env` with your wallet details:

```env
POLYMARKET_PRIVATE_KEY=your_private_key_here
POLYMARKET_WALLET=0xYOUR_WALLET_ADDRESS
DRY_RUN=true
POLYGON_RPC=https://polygon-bor-rpc.publicnode.com
```

3. (Optional) Copy and configure the exchange API config:

```bash
cp config.example.py config.py
```

### Running

Start a live trading bot:

```bash
python polymarket/kmeans_15m_trader_v1_live.py   # 15-minute BTC trader (port 5055)
python polymarket/kmeans_hourly_trader_v2_live.py # Hourly BTC trader (port 5054)
```

Open the dashboard in your browser at `http://localhost:5055` (or 5054 for hourly).

### Safety

- Set `DRY_RUN=true` in `.env` to simulate without placing real orders
- Set `DRY_RUN=false` for live trading with real money
- Daily loss limits are enforced (`MAX_DAILY_LOSS` parameter in each bot)
- Orders use GTC with a max price cap to limit slippage

## Project Structure

```
trading-scripts/
  polymarket/
    kmeans_15m_trader_v1_live.py   # Main 15m live trader
    kmeans_hourly_trader_v2_live.py # Hourly live trader
    cross_day_arb.py                # Cross-day arbitrage
    elon_tweet_arb.py               # Tweet count arbitrage
    term_structure_arb.py           # Term structure arbitrage
    ...                             # Paper traders & backtests
    .env.example                    # Environment template
  config.example.py                 # API config template
  requirements.txt                  # Python dependencies
  .gitignore
  README.md
```

## Disclaimer

This software is for educational and research purposes. Trading on prediction markets involves real financial risk. Use at your own risk. Always start with `DRY_RUN=true` and small stakes.
