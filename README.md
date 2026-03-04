# Polymarket Trading Bots

Automated trading bots for [Polymarket](https://polymarket.com) prediction markets, focused on BTC Up/Down markets and various arbitrage strategies.

## Strategies

### Live Trading Bots

| Bot | File | Description |
|-----|------|-------------|
| **15m K-Means** | `polymarket/kmeans_15m_trader_v1_live.py` | Live trader for 15-minute BTC Up/Down markets using K-means S/R clustering, intra-window momentum, and volume confirmation. Web dashboard on port 5055. |
| **Hourly K-Means** | `polymarket/kmeans_hourly_trader_v2_live.py` | Live trader for hourly BTC Up/Down markets with K-means clustering, dynamic stake sizing, and trend filters. Web dashboard on port 5054. |

### Arbitrage Bots

| Bot | File | Description |
|-----|------|-------------|
| **Cross-Day Arb** | `polymarket/cross_day_arb.py` | Exploits mispricing between today's and tomorrow's "Bitcoin above $X" markets using Black-Scholes digital option pricing. |
| **Tweet Count Arb** | `polymarket/elon_tweet_arb.py` | Trades mispricing between overlapping Elon Musk tweet-count markets using Poisson probability models. |
| **Term Structure Arb** | `polymarket/term_structure_arb.py` | Arbitrages mispricing between 15-minute and 60-minute BTC Up/Down markets using constant hazard rate assumptions. |

### Paper Trading / Research

| Bot | File | Description |
|-----|------|-------------|
| **Hourly K-Means (paper)** | `polymarket/kmeans_hourly_trader_v2.py` | Paper trading version of the hourly K-means strategy with momentum, volume, and dynamic sizing. |
| **K-Means Hourly V1** | `polymarket/kmeans_hourly_trader.py` | Original hourly K-means paper trader using 5m BTC candles. |
| **K-Means 5m** | `polymarket/kmeans_trader.py` | K-means paper trader for 5-minute BTC Up/Down markets. |
| **Tweet Momentum** | `polymarket/elon_tweets_momentum.py` | Momentum paper trader for Elon Musk tweet-count events with Kelly criterion and trailing exits. |
| **Paper Trader** | `polymarket/paper_trader.py` | Buys outcomes priced at $0.97-$0.98 within 20 minutes of close with $0.80 stop loss. |

### Backtesting

| Tool | File | Description |
|------|------|-------------|
| **97c Backtest** | `polymarket/backtest_97c.py` | Backtests the strategy of buying outcomes trading at $0.97+. |
| **97c 5-Min** | `polymarket/backtest_97c_5min.py` | Same strategy backtested on 5-minute resolution. |
| **97c 20-Min** | `polymarket/backtest_97c_20min.py` | Same strategy backtested on 20-minute resolution. |
| **Two Strategies** | `polymarket/backtest_two_strategies.py` | Compares two backtested strategies side by side. |
| **97c Analysis** | `polymarket/buy_97c_strategy.py` | Analysis tool for the $0.97+ buy strategy. |

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
