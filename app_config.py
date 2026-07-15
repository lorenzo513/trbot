import os
from functools import lru_cache


def get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@lru_cache(maxsize=1)
def get_kraken_credentials() -> tuple[str, str]:
    return (
        get_env("KRAKEN_API_KEY", required=True),
        get_env("KRAKEN_SECRET", required=True),
    )


@lru_cache(maxsize=1)
def get_telegram_credentials() -> tuple[str, str]:
    return (
        get_env("TELEGRAM_TOKEN", required=True),
        get_env("TELEGRAM_CHAT_ID", required=True),
    )


def get_trade_history_bucket() -> str:
    return get_env("TRADE_HISTORY_BUCKET", default="")


def get_trade_history_object_name() -> str:
    return get_env("TRADE_HISTORY_OBJECT", default="storico_trade.csv")


def get_trade_history_local_path() -> str:
    return get_env("TRADE_HISTORY_LOCAL_PATH", default="storico_trade.csv")


def get_trade_history_source() -> str:
    return get_env("TRADE_HISTORY_SOURCE", default="hybrid").strip().lower()


def get_trade_history_lookback_days() -> int:
    raw_value = get_env("TRADE_HISTORY_LOOKBACK_DAYS", default="365")
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 365


def get_trade_history_limit() -> int:
    raw_value = get_env("TRADE_HISTORY_LIMIT", default="500")
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 500


def get_news_sentiment_object_name() -> str:
    return get_env("NEWS_SENTIMENT_OBJECT", default="news_sentiment.json")


def get_news_sentiment_local_path() -> str:
    return get_env("NEWS_SENTIMENT_LOCAL_PATH", default="news_sentiment.json")


def get_dashboard_snapshot_object_name() -> str:
    return get_env("DASHBOARD_SNAPSHOT_OBJECT", default="dashboard_snapshot.json")


def get_dashboard_snapshot_local_path() -> str:
    return get_env("DASHBOARD_SNAPSHOT_LOCAL_PATH", default="dashboard_snapshot.json")
