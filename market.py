from functools import lru_cache

import ccxt
import pandas as pd

from app_config import get_kraken_credentials

CRYPTO_TARGETS = ["SOL/EUR", "XRP/EUR", "LINK/EUR", "ADA/EUR", "DOGE/EUR"]
TIMEFRAME = "15m"
TRADE_AMOUNT_EUR = 20.0
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
