from functools import lru_cache

import ccxt
import pandas as pd

from app_config import get_kraken_credentials

CRYPTO_TARGETS = ["SOL/EUR", "XRP/EUR", "LINK/EUR", "ADA/EUR", "DOGE/EUR", "EDGE/EUR"]
TIMEFRAME = "5m"
TRADE_AMOUNT_EUR = 100.0
LEVERAGE = 3
MAX_CONCURRENT_TRADES = 3


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


def get_market_data(symbol: str) -> pd.DataFrame:
    bars = get_exchange().fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=50)
    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["RSI"] = _rsi(df["close"], length=14)
    df["EMA_9"] = _ema(df["close"], length=9)
    return df


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


def get_open_positions() -> list[dict]:
    positions = get_exchange().fetch_positions()
    open_positions: list[dict] = []

    for position in positions:
        contracts = float(position.get("contracts") or 0)
        if contracts <= 0:
            continue
        open_positions.append(position)

    return open_positions


def get_open_position_counts() -> dict[str, int]:
    counts = {symbol: 0 for symbol in CRYPTO_TARGETS}
    for position in get_open_positions():
        symbol = position.get("symbol")
        if symbol in counts:
            counts[symbol] += 1
    return counts


def get_open_positions_count() -> int:
    return sum(get_open_position_counts().values())


def has_open_position(symbol: str) -> bool:
    return get_open_position_counts().get(symbol, 0) > 0
