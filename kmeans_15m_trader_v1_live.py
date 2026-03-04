"""
Polymarket 15-Minute BTC Up/Down — K-Means S/R LIVE Trader V1
==============================================================
REAL MONEY trading bot. Uses py-clob-client to place actual orders
on Polymarket's CLOB for the 15-minute BTC Up/Down market.

Supports DRY_RUN=true mode to simulate without placing orders.

Run:  python3 kmeans_15m_trader_v1_live.py
Open: http://localhost:5055
"""

import sqlite3
import requests
import json
import time
import threading
import logging
import os
import webbrowser
import numpy as np
import pandas as pd
import ccxt
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional
from flask import Flask, jsonify
from sklearn.cluster import KMeans
from kneed import KneeLocator
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, TradeParams
from py_clob_client.order_builder.constants import BUY
from web3 import Web3
from eth_account import Account
from eth_abi import encode

# ─── Load environment ────────────────────────────────────────────────
load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("POLYMARKET_WALLET")
PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY", WALLET_ADDRESS)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

if not PRIVATE_KEY or not WALLET_ADDRESS:
    print("\n  ERROR: Missing credentials!")
    print("  Copy .env.example to .env and fill in your wallet details:")
    print("    cp .env.example .env\n")
    exit(1)

# ─── Strategy Parameters ─────────────────────────────────────────────
TIMEFRAME = "1m"            # 1-minute candles (faster for 15m windows)
CANDLE_LIMIT = 100          # enough for signal generation
MAV_PERIOD = 10             # shorter MA for faster timeframe
BASE_STAKE = 20             # live stake (dynamic sizing scales this)
RESOLUTION_WAIT = 30        # wait 30s after window close to check result
CYCLE_SLEEP = 15            # check every 15 seconds (faster for 15m windows)
MIN_SAMPLES = 2             # fewer samples needed — enter fast
TRADE_AFTER_MINUTES = 2     # enter after 2 minutes (get in before market reprices)
CONSENSUS_THRESHOLD = 1.0
MIN_ENTRY_PRICE = 0.55      # 15m markets tend to be closer to 50/50
MAX_SLIPPAGE = 0.15         # max 15% slippage from displayed price (15m books are thin)
MAX_DAILY_LOSS = 50          # stop trading if daily loss exceeds this

# Intra-window momentum parameters
MIN_WINDOW_MOVE_PCT = 0.02  # BTC must move >= 0.02% from window open to signal
MIN_MOMENTUM_PCT = 0.03     # lower threshold — momentum is from window open
VOLUME_LOOKBACK = 10         # shorter lookback for 1m candles
VOLUME_MULTIPLIER = 0.5
MARKET_TREND_MIN_READINGS = 2
MARKET_TREND_DIRECTION = True

# Volatility filter — ATR-based
ATR_PERIOD = 14             # 14 x 1m candles for ATR
MIN_ATR_USD = 15.0          # skip if ATR < $15 (market too quiet)

# Low-volatility hours to skip (ET hours, 0-23)
# Asian session overnight = low BTC volume, mostly noise
LOW_VOL_HOURS_ET = set(range(0, 7))  # 12am-7am ET = skip

STAKE_TIERS = [
    (0.75, 2.0),
    (0.70, 1.5),
    (0.65, 1.2),
    (0.60, 1.0),
    (0.55, 0.8),
]

# ─── API endpoints ───────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# ─── On-chain config (Polygon mainnet) ──────────────────────────────
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://rpc.ankr.com/polygon")
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
NEGR_ADAPTER = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")

# Minimal ABIs for on-chain redemption
CTF_ABI = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
SAFE_ABI = [{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"stateMutability":"payable","type":"function"},{"inputs":[],"name":"nonce","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],"name":"getTransactionHash","outputs":[{"name":"","type":"bytes32"}],"stateMutability":"view","type":"function"}]

# ─── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "kmeans_15m_v1_live_trades.db")
LOG_PATH = os.path.join(SCRIPT_DIR, "kmeans_15m_v1_live.log")

# ─── CLOB Client ─────────────────────────────────────────────────────
clob_client = None


def init_clob_client():
    """Initialize the Polymarket CLOB client."""
    global clob_client
    try:
        clob_client = ClobClient(
            CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=137,       # Polygon mainnet
            signature_type=2,   # 2 = Polymarket proxy (Gnosis Safe)
            funder=PROXY_ADDRESS,
        )
        creds = clob_client.create_or_derive_api_creds()
        clob_client.set_api_creds(creds)
        log.info(f"CLOB client initialized for wallet {WALLET_ADDRESS[:10]}...")
        add_activity(f"CLOB client ready — wallet {WALLET_ADDRESS[:10]}...")
        return True
    except Exception as e:
        log.error(f"Failed to initialize CLOB client: {e}")
        add_activity(f"CLOB ERROR: {e}")
        return False


def get_wallet_balance() -> Optional[float]:
    """Get USDC balance from the CLOB client."""
    if clob_client is None:
        return None
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = clob_client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if bal and "balance" in bal:
            return float(bal["balance"]) / 1e6  # USDC has 6 decimals
    except Exception:
        pass
    return None


def get_actual_fill_price(order_response: dict, fallback_price: float) -> float:
    """Extract actual fill price from the post_order response.
    Response format: {'takingAmount': '32.307691', 'makingAmount': '20.999999', 'status': 'matched'}
    Fill price = makingAmount / takingAmount (USDC paid / shares received)."""
    if not isinstance(order_response, dict):
        return fallback_price
    try:
        taking = order_response.get("takingAmount")  # shares received
        making = order_response.get("makingAmount")  # USDC paid
        if taking and making:
            shares = float(taking)
            cost = float(making)
            if shares > 0 and cost > 0:
                fill_price = cost / shares
                if 0.01 < fill_price < 1.0:
                    log.info(f"Actual fill price: ${fill_price:.4f} (display was ${fallback_price:.4f}, "
                             f"paid ${cost:.2f} for {shares:.2f} shares)")
                    return fill_price
        # Fallback: try other field names
        for key in ("averagePrice", "average_price", "price"):
            val = order_response.get(key)
            if val is not None:
                parsed = float(val)
                if 0.01 < parsed < 1.0:
                    log.info(f"Actual fill price: ${parsed:.4f} (display was ${fallback_price:.4f})")
                    return parsed
    except Exception as e:
        log.warning(f"Failed to parse fill price: {e}")
    return fallback_price


# ─── In-memory state ─────────────────────────────────────────────────
activity_log = []
activity_lock = threading.Lock()
bot_start_time = None
wallet_balance = None
wallet_balance_lock = threading.Lock()

current_analysis = {}
analysis_lock = threading.Lock()

# Window-open BTC price tracking (the key insight for 15m markets)
window_open_price = None       # BTC price at the start of the current 15m window
window_open_slug = None        # slug of the window we recorded open price for
window_open_lock = threading.Lock()

window_signals = {}  # {slug: [list of "Up"/"Down" readings]}
window_signals_lock = threading.Lock()
last_candle_ts = None
chart_data = {"timestamps": [], "prices": [], "sr_levels": [], "mav": []}
chart_data_lock = threading.Lock()

market_price_history = {}
market_price_lock = threading.Lock()


def add_activity(msg: str):
    with activity_lock:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        activity_log.append({"time": ts, "msg": msg})
        if len(activity_log) > 200:
            activity_log.pop(0)


# ─── Logging ──────────────────────────────────────────────────────────
def setup_logging():
    logger = logging.getLogger("kmeans_15m_live")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5*1024*1024, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


log = setup_logging()


# ─── Database ─────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hour_slug TEXT NOT NULL,
            window_start TEXT,
            window_end TEXT,
            window_15m TEXT,
            market_id TEXT,
            token_id TEXT,
            prediction TEXT,
            signal TEXT,
            entry_share_price REAL,
            btc_price_at_entry REAL,
            btc_mav REAL,
            btc_support REAL,
            btc_resistance REAL,
            n_clusters INTEGER,
            exit_share_price REAL,
            result TEXT,
            stake REAL,
            pnl REAL,
            entry_time TEXT,
            exit_time TEXT,
            status TEXT DEFAULT 'OPEN',
            momentum_pct REAL,
            volume_ratio REAL,
            market_trend TEXT,
            confidence_score REAL,
            order_id TEXT,
            order_response TEXT,
            is_live INTEGER DEFAULT 0,
            condition_id TEXT,
            claimed INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_slug ON trades(hour_slug)")
    # Add new columns to existing databases (safe if they already exist)
    for col, coldef in [("condition_id", "TEXT"), ("claimed", "INTEGER DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


# ─── Daily Loss Check ────────────────────────────────────────────────
def check_daily_loss_limit() -> bool:
    """Return True if we're within daily loss limit, False if exceeded."""
    conn = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("""
        SELECT COALESCE(SUM(pnl), 0) as daily_pnl
        FROM trades WHERE status = 'CLOSED'
        AND exit_time LIKE ?
    """, (f"{today}%",)).fetchone()
    conn.close()
    daily_pnl = row["daily_pnl"]
    if daily_pnl <= -MAX_DAILY_LOSS:
        return False
    return True


# ─── Price Data ───────────────────────────────────────────────────────
exchange = ccxt.binance({"enableRateLimit": True})


def fetch_btc_candles() -> Optional[pd.DataFrame]:
    try:
        ohlcv = exchange.fetch_ohlcv("BTC/USDT", TIMEFRAME, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="ms")
        df.set_index("Timestamp", inplace=True)
        return df
    except Exception as e:
        log.warning(f"Failed to fetch BTC candles: {e}")
        return None


# ─── K-Means Clustering ──────────────────────────────────────────────
def kmeans_clustering(df: pd.DataFrame):
    df = df.dropna()
    prices = np.array(df["Close"])
    wcss = []
    max_k = min(10, len(prices) - 1)
    for i in range(1, max_k + 1):
        kmeans = KMeans(n_clusters=i, init="k-means++", max_iter=300,
                        n_init=10, random_state=0)
        kmeans.fit(prices.reshape(-1, 1))
        wcss.append(kmeans.inertia_)
    knee = KneeLocator(range(1, max_k + 1), wcss, S=1.0,
                        curve="convex", direction="decreasing")
    n_clusters = int(knee.knee) if knee.knee else 3
    kmeans = KMeans(n_clusters=n_clusters, init="k-means++", max_iter=300,
                    n_init=10, random_state=0).fit(prices.reshape(-1, 1))
    clusters = kmeans.predict(prices.reshape(-1, 1))

    minmax = []
    for i in range(n_clusters):
        minmax.append([np.inf, -np.inf])
    for i in range(len(prices)):
        cluster = clusters[i]
        if prices[i] < minmax[cluster][0]:
            minmax[cluster][0] = prices[i]
        if prices[i] > minmax[cluster][1]:
            minmax[cluster][1] = prices[i]
    supports = [sublist[0] for sublist in minmax]
    resistances = [sublist[1] for sublist in minmax]
    output = []
    s = sorted(minmax, key=lambda x: x[0])
    for i, (_min, _max) in enumerate(s):
        if i == 0:
            output.append(_min)
        if i == len(minmax) - 1:
            output.append(_max)
        else:
            output.append(sum([_max, s[i + 1][0]]) / 2)
    return clusters, supports, resistances, output, n_clusters


# ─── Momentum & Volume ───────────────────────────────────────────────
def calc_momentum(price: float, mav: float) -> float:
    if mav == 0:
        return 0.0
    return abs(price - mav) / mav * 100


def calc_window_momentum(current_price: float, open_price: float) -> float:
    """Momentum as % move from window open. Positive = up, negative = down."""
    if open_price == 0:
        return 0.0
    return (current_price - open_price) / open_price * 100


def calc_volume_ratio(df: pd.DataFrame) -> float:
    if len(df) < VOLUME_LOOKBACK + 3:
        return 1.0
    avg_vol = df["Volume"].iloc[-VOLUME_LOOKBACK-3:-3].mean()
    recent_vol = df["Volume"].iloc[-3:].mean()
    if avg_vol == 0:
        return 1.0
    return recent_vol / avg_vol


# ─── Volatility Filter ──────────────────────────────────────────────
def calc_atr(df: pd.DataFrame) -> float:
    """Calculate Average True Range over ATR_PERIOD 1m candles."""
    if len(df) < ATR_PERIOD + 1:
        return 0.0
    high = df["High"].iloc[-ATR_PERIOD:]
    low = df["Low"].iloc[-ATR_PERIOD:]
    close_prev = df["Close"].iloc[-ATR_PERIOD-1:-1]
    tr = pd.concat([
        high - low,
        (high - close_prev.values).abs(),
        (low - close_prev.values).abs(),
    ], axis=1).max(axis=1)
    return tr.mean()


def is_low_volatility_period() -> bool:
    """Check if current hour (ET) is in the low-volume skip list."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc + ET_OFFSET
    return now_et.hour in LOW_VOL_HOURS_ET


# ─── Market Price Trend ──────────────────────────────────────────────
def record_market_price(slug: str, market: dict):
    prices = get_market_prices(market)
    if not prices:
        return
    with market_price_lock:
        if slug not in market_price_history:
            market_price_history[slug] = []
        market_price_history[slug].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "up": prices.get("up", 0.5),
            "down": prices.get("down", 0.5),
        })
        if len(market_price_history[slug]) > 20:
            market_price_history[slug] = market_price_history[slug][-20:]


def check_market_trend(slug: str, signal: str) -> tuple:
    with market_price_lock:
        readings = market_price_history.get(slug, [])
    if len(readings) < MARKET_TREND_MIN_READINGS:
        return True, f"not enough data ({len(readings)} readings)"
    key = signal.lower()
    prices = [r[key] for r in readings]
    first_half = prices[:len(prices)//2]
    second_half = prices[len(prices)//2:]
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    trending = avg_second >= avg_first
    change = avg_second - avg_first
    desc = f"{'Up' if change >= 0 else 'Down'} trend ({avg_first:.3f} -> {avg_second:.3f}, {change:+.3f})"
    return trending, desc


# ─── Dynamic Stake ───────────────────────────────────────────────────
def calc_stake(share_price: float, momentum_pct: float, volume_ratio: float) -> float:
    multiplier = 1.0
    for min_price, mult in STAKE_TIERS:
        if share_price >= min_price:
            multiplier = mult
            break
    if momentum_pct > 0.15:
        multiplier += 0.5
    elif momentum_pct > 0.10:
        multiplier += 0.25
    if volume_ratio > 2.0:
        multiplier += 0.5
    elif volume_ratio > 1.5:
        multiplier += 0.25
    return round(BASE_STAKE * multiplier, 2)


def calc_confidence_score(share_price, momentum_pct, volume_ratio, market_trending):
    score = 0
    if share_price >= 0.75: score += 30
    elif share_price >= 0.70: score += 25
    elif share_price >= 0.65: score += 20
    elif share_price >= 0.55: score += 10
    else: score += 5
    if momentum_pct >= 0.15: score += 25
    elif momentum_pct >= 0.10: score += 20
    elif momentum_pct >= 0.05: score += 15
    else: score += 5
    if volume_ratio >= 2.0: score += 25
    elif volume_ratio >= 1.5: score += 20
    elif volume_ratio >= 1.0: score += 15
    else: score += 5
    if market_trending: score += 20
    return score


# ─── Signal Generation ───────────────────────────────────────────────
def get_15m_signal(df, clusters, supports, resistances, open_price=None):
    """
    Primary signal: intra-window momentum (current price vs window-open price).
    Secondary confirmation: K-means S/R and MA direction.
    The market resolves on close >= open, so we directly model that.
    """
    mav = df["Close"].rolling(window=MAV_PERIOD).mean()
    price = df["Close"].iloc[-1]
    current_mav = mav.iloc[-1]
    cluster_index = clusters[-1]
    previous_cluster_index = clusters[-2]
    support = supports[cluster_index]
    resistance = resistances[cluster_index]
    volume_ratio = calc_volume_ratio(df)

    # Primary signal: intra-window momentum (this is what the market resolves on)
    if open_price is not None and open_price > 0:
        window_mom = calc_window_momentum(price, open_price)
        ma_agrees = (price > current_mav and window_mom > 0) or (price < current_mav and window_mom < 0)

        if abs(window_mom) >= MIN_WINDOW_MOVE_PCT:
            signal = "Up" if window_mom > 0 else "Down"
            confirm = " + MA confirms" if ma_agrees else " (MA disagrees)"
            reason = (f"Window momentum {window_mom:+.4f}% | "
                      f"open=${open_price:,.2f} now=${price:,.2f}{confirm}")
        elif price > current_mav:
            signal = "Up"
            window_mom = calc_window_momentum(price, open_price)
            reason = (f"Weak move {window_mom:+.4f}%, MA tiebreak Up | "
                      f"open=${open_price:,.2f} MA=${current_mav:,.2f}")
        elif price < current_mav:
            signal = "Down"
            window_mom = calc_window_momentum(price, open_price)
            reason = (f"Weak move {window_mom:+.4f}%, MA tiebreak Down | "
                      f"open=${open_price:,.2f} MA=${current_mav:,.2f}")
        else:
            signal = "Up" if window_mom >= 0 else "Down"
            reason = f"Flat — defaulting {signal} (mom={window_mom:+.4f}%)"
        momentum_pct = abs(window_mom)
    else:
        # Fallback: no window-open price yet, use MA-based signal
        momentum_pct = calc_momentum(price, current_mav)
        if price > current_mav:
            signal = "Up"
            reason = f"No window open yet — MA fallback Up (${price:,.2f} > MA ${current_mav:,.2f})"
        elif price < current_mav:
            signal = "Down"
            reason = f"No window open yet — MA fallback Down (${price:,.2f} < MA ${current_mav:,.2f})"
        else:
            signal = "Up"
            reason = f"No window open yet — defaulting Up"

    reason += f" | vol={volume_ratio:.2f}x"
    return signal, reason, price, current_mav, support, resistance, momentum_pct, volume_ratio


# ─── Polymarket 15-Minute Market Discovery ───────────────────────────
ET_OFFSET = timedelta(hours=-5)


def _15m_window_start_utc(dt_utc: datetime) -> datetime:
    """Round a UTC datetime down to the nearest 15-minute boundary."""
    minute = (dt_utc.minute // 15) * 15
    return dt_utc.replace(minute=minute, second=0, microsecond=0)


def get_current_15m_slug() -> str:
    """Build the event slug for the current 15-minute window."""
    now_utc = datetime.now(timezone.utc)
    window_start = _15m_window_start_utc(now_utc)
    ts = int(window_start.timestamp())
    return f"btc-updown-15m-{ts}"


def get_next_15m_slug() -> str:
    """Build the event slug for the next 15-minute window."""
    now_utc = datetime.now(timezone.utc)
    window_start = _15m_window_start_utc(now_utc) + timedelta(minutes=15)
    ts = int(window_start.timestamp())
    return f"btc-updown-15m-{ts}"


def seconds_until_next_15min() -> int:
    """Seconds until the next 15-minute boundary (:00, :15, :30, :45)."""
    now = datetime.now(timezone.utc)
    current_boundary = _15m_window_start_utc(now)
    next_boundary = current_boundary + timedelta(minutes=15)
    return int((next_boundary - now).total_seconds())


def current_15m_window_et() -> tuple:
    """Return (start, end) of the current 15-minute window in ET."""
    now_utc = datetime.now(timezone.utc)
    window_start_utc = _15m_window_start_utc(now_utc)
    window_end_utc = window_start_utc + timedelta(minutes=15)
    start_et = window_start_utc + ET_OFFSET
    end_et = window_end_utc + ET_OFFSET
    return start_et, end_et


def minutes_into_15m_window() -> int:
    """How many minutes into the current 15-minute window we are."""
    now = datetime.now(timezone.utc)
    window_start = _15m_window_start_utc(now)
    return int((now - window_start).total_seconds() / 60)


def fetch_15m_market(slug: str) -> Optional[dict]:
    """Fetch the market for a given 15-minute window slug from Gamma API."""
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            markets = data[0].get("markets", [])
            if markets:
                return markets[0]
        return None
    except Exception as e:
        log.warning(f"Failed to fetch market for {slug}: {e}")
        return None


def get_market_prices(market: dict) -> dict:
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        prices = json.loads(market.get("outcomePrices", "[]"))
        result = {}
        for o, p in zip(outcomes, prices):
            result[o.lower()] = float(p)
        return result
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def is_market_resolved(market: dict) -> bool:
    return market.get("closed", False) is True


# ─── Live Trading ─────────────────────────────────────────────────────
def place_real_trade(slug: str, signal: str, reason: str,
                     btc_price: float, mav: float, support: float,
                     resistance: float, n_clusters: int, market: dict,
                     momentum_pct: float, volume_ratio: float):
    """Place a REAL trade on Polymarket via CLOB API."""
    conn = get_db()

    existing = conn.execute(
        "SELECT 1 FROM trades WHERE hour_slug = ?", (slug,)
    ).fetchone()
    if existing:
        conn.close()
        return

    try:
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        outcomes = json.loads(market.get("outcomes", "[]"))
    except (json.JSONDecodeError, TypeError):
        conn.close()
        return

    if len(token_ids) < 2 or len(outcomes) < 2:
        conn.close()
        return

    up_idx, down_idx = None, None
    for i, o in enumerate(outcomes):
        if o.lower() == "up": up_idx = i
        elif o.lower() == "down": down_idx = i
    if up_idx is None or down_idx is None:
        up_idx, down_idx = 0, 1

    if signal == "Up":
        token_idx = up_idx
        prediction = "Up"
    else:
        token_idx = down_idx
        prediction = "Down"

    token_id = token_ids[token_idx]

    prices = get_market_prices(market)
    share_price = prices.get(prediction.lower())
    if share_price is None or share_price <= 0.01 or share_price >= 0.99:
        log.info(f"Skipping {slug}: share price {share_price} out of range")
        conn.close()
        return

    if 0.48 <= share_price <= 0.52:
        log.info(f"Skipping {slug}: default 50/50 price")
        add_activity(f"SKIP {slug}: default 50/50 price ${share_price:.3f}")
        conn.close()
        return

    if share_price < MIN_ENTRY_PRICE:
        log.info(f"Skipping {slug}: price ${share_price:.3f} < min ${MIN_ENTRY_PRICE:.2f}")
        add_activity(f"SKIP {slug}: price ${share_price:.3f} < ${MIN_ENTRY_PRICE:.2f}")
        conn.close()
        return

    if momentum_pct < MIN_MOMENTUM_PCT:
        add_activity(f"SKIP {slug}: momentum {momentum_pct:.3f}% too weak")
        conn.close()
        return

    if volume_ratio < VOLUME_MULTIPLIER:
        add_activity(f"SKIP {slug}: volume {volume_ratio:.2f}x below avg")
        conn.close()
        return

    if MARKET_TREND_DIRECTION:
        trending, trend_desc = check_market_trend(slug, signal)
        if not trending:
            add_activity(f"SKIP {slug}: market disagrees — {trend_desc}")
            conn.close()
            return
    else:
        trending = True
        trend_desc = "disabled"

    # Daily loss check
    if not check_daily_loss_limit():
        add_activity(f"STOP: daily loss limit ${MAX_DAILY_LOSS} exceeded, no more trades today")
        conn.close()
        return

    stake = calc_stake(share_price, momentum_pct, volume_ratio)
    confidence = calc_confidence_score(share_price, momentum_pct, volume_ratio, trending)

    start_et, end_et = current_15m_window_et()
    window_15m = start_et.strftime("%Y-%m-%d %H:%M ET")
    market_id = market.get("id", "")
    condition_id = market.get("conditionId", "")
    now_str = datetime.now(timezone.utc).isoformat()

    # ─── PLACE THE ORDER ──────────────────────────────────────────
    order_id = None
    order_response_str = None
    is_live = 0

    if DRY_RUN:
        order_id = f"DRY_RUN_{int(time.time())}"
        order_response_str = json.dumps({"dry_run": True, "stake": stake})
        is_live = 0
        msg_prefix = "DRY RUN"
        log.info(f"DRY RUN: Would place {prediction} order for ${stake:.2f} on {slug}")
    else:
        try:
            # GTC order with price cap: order rests on book until filled
            max_price = min(share_price * (1 + MAX_SLIPPAGE), 0.99)
            max_price = round(max_price, 2)
            stake_rounded = round(stake, 2)

            log.info(f"Placing GTC order: {prediction} | display=${share_price:.4f} max=${max_price:.2f} stake=${stake_rounded:.2f}")

            order = MarketOrderArgs(
                token_id=token_id,
                amount=stake_rounded,
                price=max_price,
                side=BUY,
                order_type=OrderType.GTC,
            )
            signed = clob_client.create_market_order(order)
            response = clob_client.post_order(signed, OrderType.GTC)

            order_id = response.get("orderID", str(response))
            order_response_str = json.dumps(response) if isinstance(response, dict) else str(response)

            # GTC orders are accepted even if not immediately filled
            status = response.get("status", "")
            if status == "matched":
                is_live = 1
                msg_prefix = "LIVE ORDER FILLED"
                log.info(f"LIVE ORDER FILLED: max=${max_price:.2f} | {response}")
                share_price = get_actual_fill_price(response, share_price)
            elif status in ("live", "delayed"):
                # Order is resting on the book, waiting for a fill
                is_live = 1
                msg_prefix = "LIVE ORDER PLACED"
                log.info(f"GTC order resting on book: {response}")
            else:
                log.warning(f"Order status '{status}': {response}")
                add_activity(f"ORDER REJECTED: {status}")
                conn.execute("""
                    INSERT INTO trades
                    (hour_slug, window_start, window_end, window_15m, market_id, token_id,
                     prediction, signal, entry_share_price, btc_price_at_entry,
                     btc_mav, btc_support, btc_resistance, n_clusters,
                     stake, entry_time, status, momentum_pct, volume_ratio,
                     market_trend, confidence_score, order_id, order_response, is_live,
                     condition_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'KILLED', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (slug, start_et.strftime("%Y-%m-%d %H:%M ET"),
                      end_et.strftime("%Y-%m-%d %H:%M ET"), window_15m,
                      market_id, token_id, prediction, reason,
                      share_price, btc_price, mav, support, resistance,
                      n_clusters, stake, now_str, momentum_pct, volume_ratio,
                      trend_desc, confidence, order_id, order_response_str, 0,
                      condition_id))
                conn.commit()
                conn.close()
                return
        except Exception as e:
            log.error(f"ORDER FAILED for {slug}: {e}")
            add_activity(f"ORDER FAILED: {e}")
            # Record failed trade so we don't retry every cycle
            conn.execute("""
                INSERT INTO trades
                (hour_slug, window_start, window_end, window_15m, market_id, token_id,
                 prediction, signal, entry_share_price, btc_price_at_entry,
                 btc_mav, btc_support, btc_resistance, n_clusters,
                 stake, entry_time, status, momentum_pct, volume_ratio,
                 market_trend, confidence_score, order_id, order_response, is_live,
                 condition_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FAILED', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (slug, start_et.strftime("%Y-%m-%d %H:%M ET"),
                  end_et.strftime("%Y-%m-%d %H:%M ET"), window_15m,
                  market_id, token_id, prediction, reason,
                  share_price, btc_price, mav, support, resistance,
                  n_clusters, stake, now_str, momentum_pct, volume_ratio,
                  trend_desc, confidence, f"FAILED_{int(time.time())}",
                  json.dumps({"error": str(e)}), 0, condition_id))
            conn.commit()
            conn.close()
            return

    conn.execute("""
        INSERT INTO trades
        (hour_slug, window_start, window_end, window_15m, market_id, token_id,
         prediction, signal, entry_share_price, btc_price_at_entry,
         btc_mav, btc_support, btc_resistance, n_clusters,
         stake, entry_time, status, momentum_pct, volume_ratio,
         market_trend, confidence_score, order_id, order_response, is_live,
         condition_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (slug, start_et.strftime("%Y-%m-%d %H:%M ET"),
          end_et.strftime("%Y-%m-%d %H:%M ET"), window_15m,
          market_id, token_id, prediction, reason,
          share_price, btc_price, mav, support, resistance,
          n_clusters, stake, now_str, momentum_pct, volume_ratio,
          trend_desc, confidence, order_id, order_response_str, is_live,
          condition_id))
    conn.commit()
    conn.close()

    shares = stake / share_price
    msg = (f"{msg_prefix}: {prediction} @ ${share_price:.4f} ({shares:.1f} shares, ${stake:.0f} stake) | "
           f"BTC=${btc_price:,.2f} | conf={confidence:.0f} | "
           f"{start_et.strftime('%b %d %I:%M%p')}-{end_et.strftime('%I:%M%p')} ET")
    log.info(msg)
    add_activity(msg)


def resolve_trades():
    conn = get_db()
    open_trades = conn.execute(
        "SELECT id, hour_slug, token_id, prediction, entry_share_price, stake "
        "FROM trades WHERE status = 'OPEN'"
    ).fetchall()

    for trade in open_trades:
        slug = trade["hour_slug"]
        market = fetch_15m_market(slug)
        if market is None:
            continue
        prices = get_market_prices(market)
        pred_price = prices.get(trade["prediction"].lower())
        if pred_price is None:
            continue

        if is_market_resolved(market):
            exit_price = pred_price
            result = "WIN" if exit_price >= 0.90 else "LOSS"
        elif pred_price >= 0.95 or pred_price <= 0.05:
            exit_price = pred_price
            result = "WIN" if exit_price >= 0.90 else "LOSS"
        else:
            continue

        shares = trade["stake"] / trade["entry_share_price"]
        pnl = shares * exit_price - trade["stake"]
        now_str = datetime.now(timezone.utc).isoformat()

        conn.execute("""
            UPDATE trades SET exit_share_price = ?, result = ?, pnl = ?,
                exit_time = ?, status = 'CLOSED' WHERE id = ?
        """, (exit_price, result, pnl, now_str, trade["id"]))

        symbol = "+" if result == "WIN" else "X"
        msg = (f"[{symbol}] {result}: {trade['prediction']} | "
               f"${trade['entry_share_price']:.4f} -> ${exit_price:.4f} | "
               f"P&L=${pnl:+.2f} (stake=${trade['stake']:.0f})")
        log.info(msg)
        add_activity(msg)

    conn.commit()
    conn.close()


# ─── Auto-Claim Winnings ─────────────────────────────────────────────
_last_claim_attempt = 0  # timestamp of last claim attempt
_claim_matic_warned = False  # only warn once about missing MATIC

def claim_winnings():
    """Redeem winning positions on-chain via CTF contract through Gnosis Safe proxy.
    Requires MATIC in the EOA wallet for gas fees on Polygon."""
    global _last_claim_attempt
    if DRY_RUN:
        return
    # Only attempt claiming every 5 minutes to avoid spam
    now = time.time()
    if now - _last_claim_attempt < 300:
        return
    _last_claim_attempt = now

    conn = get_db()
    unclaimed = conn.execute(
        "SELECT id, hour_slug, token_id, condition_id, prediction, stake "
        "FROM trades WHERE status = 'CLOSED' AND result = 'WIN' "
        "AND is_live = 1 AND (claimed IS NULL OR claimed = 0) "
        "AND condition_id IS NOT NULL AND condition_id != ''"
    ).fetchall()
    conn.close()

    if not unclaimed:
        return

    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        if not w3.is_connected():
            log.warning("Cannot connect to Polygon RPC for claiming")
            return

        account = Account.from_key(PRIVATE_KEY)
        proxy_addr = Web3.to_checksum_address(PROXY_ADDRESS)
        safe = w3.eth.contract(address=proxy_addr, abi=SAFE_ABI)
        ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    except Exception as e:
        log.warning(f"Failed to init web3 for claiming: {e}")
        return

    # Check MATIC balance — needed for gas on Polygon
    global _claim_matic_warned
    try:
        matic_balance = w3.eth.get_balance(account.address)
        matic_eth = w3.from_wei(matic_balance, 'ether')
        if matic_balance == 0:
            if not _claim_matic_warned:
                log.info(
                    f"Auto-claim disabled: EOA has 0 MATIC for gas. "
                    f"Claim manually on polymarket.com, or send ~0.1 MATIC to "
                    f"{account.address} on Polygon to enable auto-claiming."
                )
                add_activity(f"Auto-claim off: no MATIC for gas (claim manually on website)")
                _claim_matic_warned = True
            return
        # MATIC appeared — reset warning so it can fire again if balance drops to 0
        _claim_matic_warned = False
        if matic_eth < 0.01:
            log.warning(f"Low MATIC balance: {matic_eth:.6f} MATIC — claims may fail soon")
    except Exception as e:
        log.warning(f"Could not check MATIC balance: {e}")
        return

    log.info(f"Attempting to claim {len(unclaimed)} winning trade(s) (MATIC balance: {matic_eth:.4f})")

    for trade in unclaimed:
        cond_id = trade["condition_id"]
        try:
            condition_bytes = bytes.fromhex(cond_id.replace("0x", ""))
            if len(condition_bytes) != 32:
                log.warning(f"Invalid condition_id length for trade {trade['id']}: {cond_id}")
                continue

            # Check if we actually have tokens to redeem
            token_id_int = int(trade["token_id"]) if trade["token_id"].isdigit() else int(trade["token_id"], 16)
            balance = ctf.functions.balanceOf(proxy_addr, token_id_int).call()
            if balance == 0:
                # Already redeemed or no position — mark as claimed
                conn = get_db()
                conn.execute("UPDATE trades SET claimed = 1 WHERE id = ?", (trade["id"],))
                conn.commit()
                conn.close()
                log.info(f"Trade {trade['id']} ({trade['hour_slug']}): no tokens to redeem, marked claimed")
                continue

            # Build redeemPositions calldata
            redeem_data = ctf.encode_abi("redeemPositions", args=[
                USDC_ADDRESS,
                b'\x00' * 32,       # parentCollectionId = bytes32(0)
                condition_bytes,
                [1, 2]              # both outcomes for binary market
            ])
            redeem_bytes = bytes.fromhex(redeem_data[2:])  # strip 0x

            # Execute through Gnosis Safe
            zero_addr = Web3.to_checksum_address("0x" + "00" * 20)
            safe_nonce = safe.functions.nonce().call()

            # Compute Safe transaction hash
            safe_tx_hash = safe.functions.getTransactionHash(
                CTF_ADDRESS, 0, redeem_bytes, 0, 0, 0, 0,
                zero_addr, zero_addr, safe_nonce
            ).call()

            # Sign with EOA (the Safe owner)
            sign_fn = getattr(account, "signHash", None) or account.unsafe_sign_hash
            signed = sign_fn(safe_tx_hash)

            sig_bytes = (
                signed.r.to_bytes(32, 'big') +
                signed.s.to_bytes(32, 'big') +
                signed.v.to_bytes(1, 'big')
            )

            # Build and send the execTransaction call
            tx = safe.functions.execTransaction(
                CTF_ADDRESS, 0, redeem_bytes, 0, 0, 0, 0,
                zero_addr, zero_addr, sig_bytes
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 300000,
                "gasPrice": w3.eth.gas_price,
                "chainId": 137,
            })

            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                conn = get_db()
                conn.execute("UPDATE trades SET claimed = 1 WHERE id = ?", (trade["id"],))
                conn.commit()
                conn.close()
                log.info(f"CLAIMED trade {trade['id']} ({trade['hour_slug']}): tx {tx_hash.hex()}")
                add_activity(f"CLAIMED winnings for {trade['hour_slug']} (tx: {tx_hash.hex()[:16]}...)")
            else:
                log.warning(f"Claim tx reverted for trade {trade['id']}: {tx_hash.hex()}")
                add_activity(f"Claim FAILED for {trade['hour_slug']} (tx reverted)")

        except Exception as e:
            err_str = str(e)
            if "insufficient funds for gas" in err_str:
                log.warning(f"CLAIM BLOCKED: not enough MATIC for gas. Send MATIC to {account.address} on Polygon.")
                add_activity(f"CLAIM BLOCKED: need MATIC for gas")
                return  # stop trying other trades too
            log.warning(f"Failed to claim trade {trade['id']} ({trade['hour_slug']}): {e}")
            continue


# ─── Main Trading Loop ───────────────────────────────────────────────
def trading_loop():
    global bot_start_time, wallet_balance
    bot_start_time = datetime.now(timezone.utc)

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log.info(f"15m V1 {mode} Trader started")
    add_activity(f"15m V1 {mode} Bot started — {'simulating orders' if DRY_RUN else 'REAL MONEY TRADING'}")

    # Initialize CLOB client
    if not DRY_RUN:
        if not init_clob_client():
            log.error("Failed to initialize CLOB client, switching to DRY_RUN")
            add_activity("CLOB init failed — running in DRY RUN mode")

    while True:
        try:
            resolve_trades()

            # Auto-claim winning positions on-chain
            if not DRY_RUN:
                try:
                    claim_winnings()
                except Exception as e:
                    log.warning(f"Claim cycle error: {e}")

            # Update wallet balance periodically
            if not DRY_RUN:
                bal = get_wallet_balance()
                if bal is not None:
                    with wallet_balance_lock:
                        wallet_balance = bal

            secs_left = seconds_until_next_15min()
            current_slug = get_current_15m_slug()
            next_slug = get_next_15m_slug()

            # ─── Track window-open BTC price ──────────────────────
            global last_candle_ts, window_open_price, window_open_slug
            df = fetch_btc_candles()
            if df is not None and len(df) >= MAV_PERIOD + 2:
                latest_ts = df.index[-1]
                price_now = df["Close"].iloc[-1]
                new_candle = (last_candle_ts is None or latest_ts != last_candle_ts)

                # Detect new 15m window and record open price
                with window_open_lock:
                    if window_open_slug != current_slug:
                        window_open_price = price_now
                        window_open_slug = current_slug
                        log.info(f"NEW WINDOW {current_slug} — open price: ${price_now:,.2f}")
                        add_activity(f"New 15m window — BTC open: ${price_now:,.2f}")
                    current_open = window_open_price

                # Volatility filter: ATR check
                atr = calc_atr(df)
                low_vol_hour = is_low_volatility_period()

                clusters, supports, resistances, output, n_clusters = kmeans_clustering(df)
                signal, reason, price, mav, support, resistance, momentum_pct, volume_ratio = (
                    get_15m_signal(df, clusters, supports, resistances, open_price=current_open)
                )

                mins_in = minutes_into_15m_window()

                market = fetch_15m_market(current_slug)
                if market:
                    record_market_price(current_slug, market)

                with analysis_lock:
                    current_analysis.update({
                        "price": price, "mav": mav,
                        "support": support, "resistance": resistance,
                        "supports": sorted(supports), "resistances": sorted(resistances),
                        "sr_levels": sorted(output), "n_clusters": n_clusters,
                        "signal": signal, "reason": reason,
                        "current_slug": current_slug, "next_slug": next_slug,
                        "secs_left": secs_left,
                        "momentum_pct": momentum_pct, "volume_ratio": volume_ratio,
                        "window_open_price": current_open,
                        "atr": atr, "low_vol_hour": low_vol_hour,
                        "updated": datetime.now(timezone.utc).isoformat(),
                    })

                with chart_data_lock:
                    chart_slice = df.tail(60)
                    mav_series = df["Close"].rolling(window=MAV_PERIOD).mean().tail(60)
                    chart_data["timestamps"] = [t.strftime("%H:%M") for t in chart_slice.index]
                    chart_data["prices"] = chart_slice["Close"].tolist()
                    chart_data["mav"] = [round(v, 2) if not pd.isna(v) else None for v in mav_series.tolist()]
                    chart_data["sr_levels"] = sorted(output)

                trending, trend_desc = check_market_trend(current_slug, signal)
                with market_price_lock:
                    mkt_readings = len(market_price_history.get(current_slug, []))
                with analysis_lock:
                    current_analysis.update({
                        "market_trending": trending,
                        "market_trend_desc": trend_desc,
                        "market_readings": mkt_readings,
                    })

                if new_candle:
                    last_candle_ts = latest_ts
                    with window_signals_lock:
                        if current_slug not in window_signals:
                            window_signals[current_slug] = []
                        window_signals[current_slug].append(signal)
                        samples = list(window_signals[current_slug])
                        for old_slug in list(window_signals.keys()):
                            if old_slug != current_slug and old_slug != next_slug:
                                del window_signals[old_slug]
                    with market_price_lock:
                        for old_slug in list(market_price_history.keys()):
                            if old_slug != current_slug and old_slug != next_slug:
                                del market_price_history[old_slug]
                    candle_time = latest_ts.strftime("%H:%M")
                    window_chg = calc_window_momentum(price, current_open) if current_open else 0
                    msg = (f"CANDLE {candle_time} → #{len(samples)}: {signal} | "
                           f"BTC=${price:,.2f} (open=${current_open:,.2f} {window_chg:+.3f}%) | "
                           f"ATR=${atr:.0f} vol={volume_ratio:.1f}x")
                    log.info(msg)
                    add_activity(msg)
                else:
                    with window_signals_lock:
                        samples = list(window_signals.get(current_slug, []))

                n_samples = len(samples)
                up_count = samples.count("Up")
                down_count = samples.count("Down")
                up_pct = up_count / n_samples if n_samples > 0 else 0
                down_pct = down_count / n_samples if n_samples > 0 else 0
                consensus = "Up" if up_count >= down_count else "Down"
                consensus_pct = max(up_pct, down_pct)

                with analysis_lock:
                    current_analysis.update({
                        "samples": n_samples, "up_count": up_count,
                        "down_count": down_count, "consensus": consensus,
                        "consensus_pct": consensus_pct, "minutes_in": mins_in,
                    })

                if not new_candle:
                    window_chg = calc_window_momentum(price, current_open) if current_open else 0
                    add_activity(f"Tick: {consensus} {consensus_pct:.0%} ({n_samples}s) | "
                                 f"BTC {window_chg:+.3f}% from open | ATR=${atr:.0f}")

                already_traded = False
                conn_check = get_db()
                existing = conn_check.execute(
                    "SELECT 1 FROM trades WHERE hour_slug = ? AND status IN ('OPEN', 'CLOSED')",
                    (current_slug,)
                ).fetchone()
                conn_check.close()
                already_traded = existing is not None

                # ─── Trade entry with volatility filters ──────────
                if (not already_traded
                        and n_samples >= MIN_SAMPLES
                        and mins_in >= TRADE_AFTER_MINUTES
                        and consensus_pct >= CONSENSUS_THRESHOLD):

                    # Volatility gate: skip quiet markets
                    if low_vol_hour:
                        now_et = datetime.now(timezone.utc) + ET_OFFSET
                        add_activity(f"SKIP: low-vol hour ({now_et.hour}:00 ET)")
                    elif atr < MIN_ATR_USD:
                        add_activity(f"SKIP: ATR ${atr:.0f} < ${MIN_ATR_USD:.0f} (too quiet)")
                    elif not DRY_RUN:
                        # Balance check: ensure we have enough USDC to trade
                        with wallet_balance_lock:
                            cur_bal = wallet_balance
                        if cur_bal is not None and cur_bal < BASE_STAKE * 0.5:
                            add_activity(f"SKIP: balance ${cur_bal:.2f} too low (need ${BASE_STAKE * 0.5:.0f}+) — claim pending wins")
                        else:
                            consensus_reason = (
                                f"Consensus {consensus} ({up_count}U/{down_count}D over {n_samples} samples, "
                                f"{consensus_pct:.0%}) | ATR=${atr:.0f} | {reason}"
                            )
                            if market and not is_market_resolved(market):
                                place_real_trade(current_slug, consensus, consensus_reason,
                                                price, mav, support, resistance, n_clusters,
                                                market, momentum_pct, volume_ratio)
                            else:
                                add_activity(f"No active market found for {current_slug}")
                    else:
                        consensus_reason = (
                            f"Consensus {consensus} ({up_count}U/{down_count}D over {n_samples} samples, "
                            f"{consensus_pct:.0%}) | ATR=${atr:.0f} | {reason}"
                        )
                        if market and not is_market_resolved(market):
                            place_real_trade(current_slug, consensus, consensus_reason,
                                            price, mav, support, resistance, n_clusters,
                                            market, momentum_pct, volume_ratio)
                        else:
                            add_activity(f"No active market found for {current_slug}")
                elif not already_traded:
                    if n_samples < MIN_SAMPLES:
                        add_activity(f"Collecting samples... {n_samples}/{MIN_SAMPLES}")
                    elif mins_in < TRADE_AFTER_MINUTES:
                        add_activity(f"Waiting... {mins_in}/{TRADE_AFTER_MINUTES} min")
                    elif consensus_pct < CONSENSUS_THRESHOLD:
                        add_activity(f"No consensus: {consensus_pct:.0%} < {CONSENSUS_THRESHOLD:.0%}")
            else:
                add_activity("Failed to fetch BTC candles, retrying...")

            time.sleep(CYCLE_SLEEP)

        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            add_activity(f"Error: {e}")
            time.sleep(15)


# ─── Flask Web Dashboard ─────────────────────────────────────────────
app = Flask(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>BTC 15-Min K-Means V1 %(mode_upper)s</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 5px; }
  .subtitle { color: #8b949e; margin-bottom: 20px; font-size: 14px; }
  .live-badge { background: #f85149; color: white; padding: 3px 10px; border-radius: 4px;
                font-size: 12px; font-weight: bold; margin-left: 10px; }
  .dry-badge { background: #d29922; color: white; padding: 3px 10px; border-radius: 4px;
               font-size: 12px; font-weight: bold; margin-left: 10px; }
  .stats { display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap; }
  .stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
               padding: 15px 20px; min-width: 140px; }
  .stat-card .label { color: #8b949e; font-size: 12px; text-transform: uppercase; }
  .stat-card .value { font-size: 24px; font-weight: bold; margin-top: 4px; }
  .positive { color: #3fb950; }
  .negative { color: #f85149; }
  .neutral { color: #58a6ff; }
  table { width: 100%%%%; border-collapse: collapse; margin-bottom: 25px; }
  th { background: #161b22; color: #8b949e; text-align: left; padding: 10px 12px;
       font-size: 12px; text-transform: uppercase; border-bottom: 1px solid #30363d; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; font-size: 14px; }
  tr:hover { background: #161b22; }
  .section-title { color: #58a6ff; font-size: 16px; margin: 20px 0 10px;
                   border-bottom: 1px solid #30363d; padding-bottom: 5px; }
  .badge { padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
  .badge-up { background: #0d2818; color: #3fb950; }
  .badge-down { background: #2d1215; color: #f85149; }
  .badge-win { background: #0d2818; color: #3fb950; }
  .badge-loss { background: #2d1215; color: #f85149; }
  .badge-open { background: #0d1d30; color: #58a6ff; }
  .sr-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px;
            font-family: monospace; display: inline-block; margin: 2px; }
  .sr-support { background: #0d2818; color: #3fb950; }
  .analysis-box { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 20px; margin-bottom: 25px; }
  .analysis-row { display: flex; gap: 30px; flex-wrap: wrap; margin-bottom: 10px; }
  .analysis-item { font-size: 14px; }
  .analysis-item .alabel { color: #8b949e; font-size: 12px; }
  .analysis-item .avalue { font-size: 18px; font-weight: bold; margin-top: 2px; }
  .log { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
         padding: 12px; font-family: monospace; font-size: 13px;
         max-height: 400px; overflow-y: auto; }
  .log-entry { padding: 3px 0; border-bottom: 1px solid #21262d; }
  .log-time { color: #8b949e; }
  .empty { color: #8b949e; text-align: center; padding: 30px; font-style: italic; }
  .params { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            padding: 12px 16px; margin-bottom: 20px; display: flex; gap: 25px;
            flex-wrap: wrap; font-size: 13px; }
  .params span { color: #8b949e; }
  .params strong { color: #c9d1d9; }
  .countdown { font-size: 28px; color: #58a6ff; font-weight: bold; }
  .indicator { display: inline-block; width: 10px; height: 10px; border-radius: 50%%%%;
               margin-right: 5px; }
  .indicator-green { background: #3fb950; }
  .indicator-red { background: #f85149; }
  .indicator-yellow { background: #d29922; }
</style>
</head>
<body>

<h1>BTC 15-Min Up/Down — K-Means V1 <span class="%(mode_badge_class)s">%(mode_upper)s</span></h1>
<div class="subtitle">%(mode_desc)s &bull; Port 5055 &bull; Auto-refreshes 30s</div>

<div class="params">
  <div><span>Candles:</span> <strong>%(candle_limit)s x 1m</strong></div>
  <div><span>MA:</span> <strong>%(mav_period)s</strong></div>
  <div><span>Base Stake:</span> <strong>$%(base_stake)s</strong></div>
  <div><span>Max Loss/Day:</span> <strong>$%(max_daily_loss)s</strong></div>
  <div><span>Min Entry:</span> <strong>$%(min_entry)s+</strong></div>
  <div><span>Wallet:</span> <strong>%(wallet_display)s</strong></div>
  <div><span>Balance:</span> <strong>%(balance_display)s</strong></div>
  <div><span>Uptime:</span> <strong>%(uptime)s</strong></div>
</div>

<div class="stats">
  <div class="stat-card">
    <div class="label">Total Trades</div>
    <div class="value">%(total_trades)s</div>
  </div>
  <div class="stat-card">
    <div class="label">Wins</div>
    <div class="value positive">%(wins)s</div>
  </div>
  <div class="stat-card">
    <div class="label">Losses</div>
    <div class="value negative">%(losses)s</div>
  </div>
  <div class="stat-card">
    <div class="label">Win Rate</div>
    <div class="value %(wr_class)s">%(win_rate)s%%%%%%%%</div>
  </div>
  <div class="stat-card">
    <div class="label">Net P&L</div>
    <div class="value %(pnl_class)s">$%(net_pnl)s</div>
  </div>
  <div class="stat-card">
    <div class="label">Open</div>
    <div class="value neutral">%(open_count)s</div>
  </div>
</div>

<div class="section-title">Current Analysis</div>
%(analysis_html)s

<div class="section-title">Price Chart &amp; S/R Levels</div>
<div class="analysis-box" style="padding: 15px; position: relative; height: 350px;">
  <canvas id="priceChart"></canvas>
</div>

<div class="section-title">Open Trades</div>
%(open_table)s

<div class="section-title">Recent Trades (Last 50)</div>
%(closed_table)s

<div class="section-title">Activity Log</div>
<div class="log">%(activity_html)s</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
(function() {
  var priceChart = null;
  function loadChart() {
    if (typeof Chart === 'undefined') { setTimeout(loadChart, 500); return; }
    fetch('/api/chart')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.timestamps || data.timestamps.length === 0) return;
        var canvas = document.getElementById('priceChart');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        var srDatasets = data.sr_levels.map(function(level, i) {
          return { label: 'S/R $' + level.toFixed(0),
            data: new Array(data.timestamps.length).fill(level),
            borderColor: i < data.sr_levels.length / 2 ? '#3fb950' : '#f85149',
            borderWidth: 1, borderDash: [6, 3], pointRadius: 0, fill: false };
        });
        var datasets = [
          { label: 'BTC Price', data: data.prices, borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.1)', borderWidth: 2,
            pointRadius: 0, fill: true, tension: 0.3 },
          { label: 'MA(%(mav_period)s)', data: data.mav, borderColor: '#d29922',
            borderWidth: 1.5, pointRadius: 0, borderDash: [4, 2], fill: false, tension: 0.3 }
        ].concat(srDatasets);
        if (priceChart) { priceChart.destroy(); }
        priceChart = new Chart(ctx, {
          type: 'line', data: { labels: data.timestamps, datasets: datasets },
          options: { responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { intersect: false, mode: 'index' },
            scales: { x: { ticks: { color: '#8b949e', maxTicksLimit: 12 }, grid: { color: '#21262d' } },
              y: { ticks: { color: '#8b949e', callback: function(v) { return '$' + v.toLocaleString(); } },
                   grid: { color: '#21262d' } } },
            plugins: { legend: { labels: { color: '#c9d1d9', boxWidth: 12 } },
              tooltip: { callbacks: { label: function(c) { return c.dataset.label + ': $' +
                c.parsed.y.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}); } } } } }
        });
      }).catch(function(err) { console.error('Chart error:', err); });
  }
  loadChart();
  setInterval(loadChart, 15000);
  setTimeout(function(){ location.reload(); }, 30000);
})();
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    conn = get_db()
    stats = conn.execute("""
        SELECT COUNT(*) AS total,
            SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(pnl), 0) AS total_pnl
        FROM trades WHERE status = 'CLOSED'
    """).fetchone()

    open_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'").fetchone()[0]

    open_trades = conn.execute(
        "SELECT prediction, signal, entry_share_price, btc_price_at_entry, "
        "       window_start, window_end, entry_time, stake, hour_slug, "
        "       confidence_score, order_id, is_live "
        "FROM trades WHERE status = 'OPEN' ORDER BY entry_time DESC"
    ).fetchall()

    closed_trades = conn.execute(
        "SELECT prediction, signal, entry_share_price, exit_share_price, "
        "       btc_price_at_entry, result, pnl, window_start, "
        "       entry_time, exit_time, stake, hour_slug, "
        "       confidence_score, order_id, is_live "
        "FROM trades WHERE status = 'CLOSED' ORDER BY exit_time DESC LIMIT 50"
    ).fetchall()
    conn.close()

    total = stats["total"] or 0
    wins = stats["wins"] or 0
    losses = stats["losses"] or 0
    total_pnl = stats["total_pnl"] or 0
    wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    if bot_start_time:
        delta = datetime.now(timezone.utc) - bot_start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        uptime = f"{hours}h {mins}m {secs}s"
    else:
        uptime = "starting..."

    with wallet_balance_lock:
        bal = wallet_balance
    balance_display = f"${bal:,.2f}" if bal is not None else ("N/A (dry run)" if DRY_RUN else "loading...")
    wallet_display = f"{WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}" if WALLET_ADDRESS else "N/A"

    with analysis_lock:
        a = dict(current_analysis)

    if a:
        sr_tags = ""
        for lvl in a.get("sr_levels", []):
            sr_tags += f'<span class="sr-tag sr-support">${lvl:,.2f}</span>'
        sig_class = "badge-up" if a.get("signal") == "Up" else "badge-down"
        cons = a.get("consensus", "-")
        cons_class = "badge-up" if cons == "Up" else "badge-down"
        cons_pct = a.get("consensus_pct", 0)
        n_samp = a.get("samples", 0)
        up_c = a.get("up_count", 0)
        down_c = a.get("down_count", 0)
        mins_in = a.get("minutes_in", 0)
        secs = seconds_until_next_15min()
        mins_left = secs // 60
        secs_rem = secs % 60
        mom = a.get("momentum_pct", 0)
        vol_r = a.get("volume_ratio", 0)
        mkt_trend = a.get("market_trending", None)
        mkt_desc = a.get("market_trend_desc", "-")
        mkt_readings = a.get("market_readings", 0)
        w_open = a.get("window_open_price", 0)
        atr_val = a.get("atr", 0)
        low_vol = a.get("low_vol_hour", False)
        window_chg = calc_window_momentum(a.get("price", 0), w_open) if w_open else 0
        window_chg_color = "positive" if window_chg >= 0 else "negative"

        mom_color = "indicator-green" if mom >= MIN_MOMENTUM_PCT else "indicator-red"
        vol_color = "indicator-green" if vol_r >= VOLUME_MULTIPLIER else "indicator-red"
        mkt_color = "indicator-green" if mkt_trend else ("indicator-red" if mkt_trend is False else "indicator-yellow")
        atr_color = "indicator-green" if atr_val >= MIN_ATR_USD else "indicator-red"
        hour_color = "indicator-red" if low_vol else "indicator-green"

        ready = n_samp >= MIN_SAMPLES and mins_in >= TRADE_AFTER_MINUTES and cons_pct >= CONSENSUS_THRESHOLD
        blocked = False
        if ready:
            if low_vol:
                status_html = '<span style="color:#f85149;font-weight:bold;">BLOCKED: Low-vol hour</span>'
                blocked = True
            elif atr_val < MIN_ATR_USD:
                status_html = f'<span style="color:#f85149;font-weight:bold;">BLOCKED: ATR ${atr_val:.0f} &lt; ${MIN_ATR_USD:.0f}</span>'
                blocked = True
            else:
                status_html = '<span style="color:#3fb950;font-weight:bold;">READY TO TRADE</span>'
        else:
            reasons = []
            if n_samp < MIN_SAMPLES: reasons.append(f"samples {n_samp}/{MIN_SAMPLES}")
            if mins_in < TRADE_AFTER_MINUTES: reasons.append(f"wait {TRADE_AFTER_MINUTES - mins_in}min")
            if cons_pct < CONSENSUS_THRESHOLD: reasons.append(f"consensus {cons_pct:.0%} < {CONSENSUS_THRESHOLD:.0%}")
            status_html = f'<span style="color:#d29922;">SAMPLING: {", ".join(reasons)}</span>'

        analysis_html = f"""
        <div class="analysis-box">
          <div class="analysis-row">
            <div class="analysis-item"><div class="alabel">BTC Price</div><div class="avalue">${a.get('price', 0):,.2f}</div></div>
            <div class="analysis-item"><div class="alabel">Window Open</div><div class="avalue">${w_open:,.2f}</div></div>
            <div class="analysis-item"><div class="alabel">Window Move</div><div class="avalue {window_chg_color}">{window_chg:+.4f}%</div></div>
            <div class="analysis-item"><div class="alabel">MA({MAV_PERIOD})</div><div class="avalue">${a.get('mav', 0):,.2f}</div></div>
            <div class="analysis-item"><div class="alabel">Latest Signal</div><div class="avalue"><span class="badge {sig_class}">{a.get('signal', '-')}</span></div></div>
            <div class="analysis-item"><div class="alabel">Consensus ({n_samp} samples)</div><div class="avalue"><span class="badge {cons_class}">{cons} ({cons_pct:.0%})</span></div></div>
            <div class="analysis-item"><div class="alabel">Votes</div><div class="avalue" style="font-size:16px;"><span class="positive">{up_c}U</span> / <span class="negative">{down_c}D</span></div></div>
            <div class="analysis-item"><div class="alabel">Next 15m</div><div class="countdown">{mins_left}m {secs_rem}s</div></div>
          </div>
          <div class="analysis-row" style="margin-top:10px;">
            <div class="analysis-item"><div class="alabel"><span class="indicator {atr_color}"></span>ATR (volatility)</div><div class="avalue" style="font-size:16px;">${atr_val:.0f}</div></div>
            <div class="analysis-item"><div class="alabel"><span class="indicator {hour_color}"></span>Session</div><div class="avalue" style="font-size:16px;">{'LOW VOL' if low_vol else 'Active'}</div></div>
            <div class="analysis-item"><div class="alabel"><span class="indicator {vol_color}"></span>Volume</div><div class="avalue" style="font-size:16px;">{vol_r:.2f}x avg</div></div>
            <div class="analysis-item"><div class="alabel"><span class="indicator {mkt_color}"></span>Market Trend ({mkt_readings} readings)</div><div class="avalue" style="font-size:14px;">{mkt_desc}</div></div>
          </div>
          <div style="margin-top: 10px;">{status_html}</div>
          <div style="margin-top: 8px;"><span style="color: #8b949e; font-size: 12px;">REASON:</span> <span style="font-size: 13px;">{a.get('reason', '-')}</span></div>
          <div style="margin-top: 8px;"><span style="color: #8b949e; font-size: 12px;">S/R LEVELS:</span> {sr_tags}</div>
        </div>"""
    else:
        analysis_html = "<div class='empty'>Waiting for first analysis...</div>"

    if open_trades:
        rows = ""
        for t in open_trades:
            badge = "badge-up" if t["prediction"] == "Up" else "badge-down"
            ws = t["window_start"] or ""
            we = t["window_end"] or ""
            conf = t["confidence_score"] or 0
            live_tag = "LIVE" if t["is_live"] else "DRY"
            rows += (
                f"<tr><td><span class='badge {badge}'>{t['prediction']}</span></td>"
                f"<td>${t['entry_share_price']:.4f}</td>"
                f"<td>${t['btc_price_at_entry']:,.2f}</td>"
                f"<td>${t['stake']:.0f}</td><td>{conf:.0f}</td>"
                f"<td>{live_tag}</td>"
                f"<td>{ws} → {we}</td>"
                f"<td><span class='badge badge-open'>OPEN</span></td></tr>"
            )
        open_table = (
            "<table><th>Prediction</th><th>Share</th><th>BTC</th>"
            f"<th>Stake</th><th>Conf</th><th>Mode</th><th>Window</th><th>Status</th>{rows}</table>"
        )
    else:
        open_table = "<div class='empty'>No open trades</div>"

    if closed_trades:
        rows = ""
        for t in closed_trades:
            pred_badge = "badge-up" if t["prediction"] == "Up" else "badge-down"
            res_badge = "badge-win" if t["result"] == "WIN" else "badge-loss"
            pnl = t["pnl"] or 0
            pnl_class = "positive" if pnl >= 0 else "negative"
            ws = t["window_start"] or ""
            conf = t["confidence_score"] or 0
            live_tag = "LIVE" if t["is_live"] else "DRY"
            rows += (
                f"<tr><td><span class='badge {pred_badge}'>{t['prediction']}</span></td>"
                f"<td>${t['entry_share_price']:.4f}</td>"
                f"<td>${t['exit_share_price']:.4f}</td>"
                f"<td>${t['btc_price_at_entry']:,.2f}</td>"
                f"<td>${t['stake']:.0f}</td><td>{conf:.0f}</td>"
                f"<td>{live_tag}</td><td>{ws}</td>"
                f"<td><span class='badge {res_badge}'>{t['result']}</span></td>"
                f"<td class='{pnl_class}'>${pnl:+.2f}</td></tr>"
            )
        closed_table = (
            "<table><th>Prediction</th><th>Entry</th><th>Exit</th>"
            f"<th>BTC</th><th>Stake</th><th>Conf</th><th>Mode</th><th>Window</th><th>Result</th><th>P&L</th>{rows}</table>"
        )
    else:
        closed_table = "<div class='empty'>No closed trades yet</div>"

    with activity_lock:
        if activity_log:
            entries = ""
            for a in reversed(activity_log[-40:]):
                entries += (f"<div class='log-entry'>"
                            f"<span class='log-time'>{a['time']}</span> {a['msg']}</div>")
            activity_html = entries
        else:
            activity_html = "<div class='empty'>No activity yet</div>"

    mode_upper = "DRY RUN" if DRY_RUN else "LIVE"
    mode_badge_class = "dry-badge" if DRY_RUN else "live-badge"
    mode_desc = "Simulating orders (no real money)" if DRY_RUN else "REAL MONEY — placing actual orders on Polymarket"

    html = HTML_TEMPLATE % {
        "mode_upper": mode_upper,
        "mode_badge_class": mode_badge_class,
        "mode_desc": mode_desc,
        "candle_limit": CANDLE_LIMIT,
        "mav_period": MAV_PERIOD,
        "base_stake": BASE_STAKE,
        "max_daily_loss": MAX_DAILY_LOSS,
        "min_entry": f"{MIN_ENTRY_PRICE:.2f}",
        "wallet_display": wallet_display,
        "balance_display": balance_display,
        "uptime": uptime,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": f"{wr:.1f}",
        "wr_class": "positive" if wr >= 55 else "negative" if wr < 45 else "neutral",
        "net_pnl": f"{total_pnl:+.2f}",
        "pnl_class": "positive" if total_pnl >= 0 else "negative",
        "open_count": open_count,
        "analysis_html": analysis_html,
        "open_table": open_table,
        "closed_table": closed_table,
        "activity_html": activity_html,
    }
    return html


@app.route("/api/chart")
def api_chart():
    with chart_data_lock:
        return jsonify(chart_data)


# ─── Main ─────────────────────────────────────────────────────────────
def main():
    init_db()

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log.info("=" * 60)
    log.info(f"BTC 15-Min Up/Down — K-Means V1 {mode} Trader")
    log.info(f"  Dashboard: http://localhost:5055")
    log.info(f"  Mode: {mode}")
    log.info(f"  Wallet: {WALLET_ADDRESS[:10]}...")
    log.info(f"  Base Stake: ${BASE_STAKE} (dynamic)")
    log.info(f"  Max Daily Loss: ${MAX_DAILY_LOSS}")
    log.info("=" * 60)

    trader = threading.Thread(target=trading_loop, daemon=True)
    trader.start()

    print(f"\n  BTC 15-Min V1 {mode} Trader running!")
    print(f"  Open http://localhost:5055 in your browser\n")

    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5055")).start()

    app.run(host="0.0.0.0", port=5055, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
