import time
from functools import lru_cache

import ccxt
import pandas as pd
import requests

from app_config import get_bool_env, get_env
from app_config import get_kraken_credentials

CORE_CRYPTO_TARGETS = [
    "BTC/EUR",
    "SOL/EUR",
    "XRP/EUR",
    "LINK/EUR",
    "ADA/EUR",
    "DOGE/EUR",
    "EDGE/EUR",
]

# Backward-compatible alias used across the project.
CRYPTO_TARGETS = CORE_CRYPTO_TARGETS

TIMEFRAME = "10m"
TRADE_AMOUNT_EUR = 20.0
LEVERAGE = 3
MAX_CONCURRENT_TRADES = 3

DEFAULT_STOP_LOSS_MULTIPLIER = 0.98
DEFAULT_TAKE_PROFIT_MULTIPLIER = 1.04

KRAKEN_BASE_ALIASES = {
    "BTC": ("BTC", "XBT"),
    "XBT": ("BTC", "XBT"),
}

_trending_cache: tuple[float, list[str]] | None = None


def get_trending_crypto_limit() -> int:
    raw_value = get_env("TRENDING_CRYPTO_LIMIT", default="5")
    try:
        return max(1, min(15, int(raw_value)))
    except ValueError:
        return 5


def is_dynamic_crypto_enabled() -> bool:
    return get_bool_env("DYNAMIC_CRYPTO_ENABLED", default=True)


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False).mean()


@lru_cache(maxsize=1)
def get_exchange() -> ccxt.kraken:
    api_key, secret = get_kraken_credentials()
    return ccxt.kraken(
        {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
        }
    )


@lru_cache(maxsize=1)
def get_kraken_eur_symbols() -> frozenset[str]:
    exchange = get_exchange()
    markets = exchange.load_markets()
    return frozenset(
        symbol
        for symbol, market in markets.items()
        if symbol.endswith("/EUR") and market.get("active", True)
    )


def _resolve_kraken_symbol(base_symbol: str) -> str | None:
    base_symbol = base_symbol.upper()
    candidates = KRAKEN_BASE_ALIASES.get(base_symbol, (base_symbol,))
    available = get_kraken_eur_symbols()

    for candidate in candidates:
        pair = f"{candidate}/EUR"
        if pair in available:
            return pair
    return None


def fetch_trending_kraken_symbols(limit: int | None = None) -> list[str]:
    global _trending_cache

    if limit is None:
        limit = get_trending_crypto_limit()

    cache_ttl = int(get_env("TRENDING_CRYPTO_CACHE_SECONDS", default="3600"))
    now = time.time()
    if _trending_cache and now - _trending_cache[0] < cache_ttl:
        return _trending_cache[1][:limit]

    trending: list[str] = []
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"Impossibile recuperare le crypto trending da CoinGecko: {exc}")
        if _trending_cache:
            return _trending_cache[1][:limit]
        return []

    for entry in payload.get("coins", []):
        item = entry.get("item", {})
        base_symbol = str(item.get("symbol", "")).upper()
        if not base_symbol:
            continue

        pair = _resolve_kraken_symbol(base_symbol)
        if pair and pair not in trending:
            trending.append(pair)

        if len(trending) >= limit:
            break

    _trending_cache = (now, trending)
    return trending[:limit]


def get_all_candidate_symbols() -> list[str]:
    symbols = list(CORE_CRYPTO_TARGETS)

    if is_dynamic_crypto_enabled():
        for symbol in fetch_trending_kraken_symbols():
            if symbol not in symbols:
                symbols.append(symbol)

    return symbols


def is_positive_trend(df: pd.DataFrame) -> bool:
    if len(df) < 5:
        return False

    last_row = df.iloc[-1]
    current_price = float(last_row["close"])
    ema_now = float(last_row["EMA_9"])
    ema_prev = float(df.iloc[-4]["EMA_9"])

    if current_price <= ema_now:
        return False
    if ema_now < ema_prev:
        return False

    price_change = (current_price - float(df.iloc[-5]["close"])) / float(df.iloc[-5]["close"])
    return price_change > 0


def get_volatility_pct(df: pd.DataFrame) -> float:
    last_row = df.iloc[-1]
    close = float(last_row["close"])
    if close <= 0:
        return 0.0
    return float(last_row["ATR"]) / close * 100


def get_risk_multipliers(volatility_pct: float) -> tuple[float, float]:
    if volatility_pct < 1.0:
        return 0.99, 1.025
    if volatility_pct < 2.5:
        return DEFAULT_STOP_LOSS_MULTIPLIER, DEFAULT_TAKE_PROFIT_MULTIPLIER
    stop_loss = max(0.90, 1 - (volatility_pct / 100) * 2.5)
    take_profit = min(1.20, 1 + (volatility_pct / 100) * 3.5)
    return stop_loss, take_profit


def get_market_data(symbol: str) -> pd.DataFrame:
    bars = get_exchange().fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=50)
    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["RSI"] = _rsi(df["close"], length=14)
    df["EMA_9"] = _ema(df["close"], length=9)
    df["ATR"] = _atr(df, length=14)
    return df


def get_active_trading_targets() -> list[str]:
    active: list[str] = []

    for symbol in get_all_candidate_symbols():
        try:
            df = get_market_data(symbol)
            if is_positive_trend(df):
                active.append(symbol)
            else:
                print(f"{symbol} escluso: trend non positivo.")
        except Exception as exc:
            print(f"Impossibile valutare il trend di {symbol}: {exc}")

    return active


def get_account_balance() -> float:
    balance = get_exchange().fetch_balance()
    return float(balance["free"].get("EUR", 0))


def get_balance_snapshot() -> dict:
    balance = get_exchange().fetch_balance()
    eur = balance.get("EUR", {})
    return {
        "free_eur": float(balance["free"].get("EUR", 0)),
        "used_eur": float(balance["used"].get("EUR", 0)),
        "total_eur": float(balance["total"].get("EUR", 0)),
        "raw": balance,
    }


def _normalize_symbol(symbol: str | None) -> str:
    if not symbol:
        return ""
    return str(symbol).split(":")[0]


def get_open_positions() -> list[dict]:
    positions = get_exchange().fetch_positions(params={"docalcs": True})
    open_positions: list[dict] = []

    for position in positions:
        contracts = float(position.get("contracts") or position.get("contractSize") or 0)
        amount = float(position.get("amount") or 0)
        size = max(contracts, amount)
        if size <= 0:
            continue
        open_positions.append(position)

    return open_positions


def get_open_position_counts() -> dict[str, int]:
    counts = {symbol: 0 for symbol in get_all_candidate_symbols()}
    for position in get_open_positions():
        symbol = _normalize_symbol(position.get("symbol"))
        if symbol in counts:
            counts[symbol] += 1
        elif symbol:
            counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def get_open_positions_count() -> int:
    return sum(get_open_position_counts().values())


def has_open_position(symbol: str) -> bool:
    return get_open_position_counts().get(symbol, 0) > 0
