from functools import lru_cache
import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import storage

from app_config import (
    get_trade_history_bucket,
    get_trade_history_limit,
    get_trade_history_local_path,
    get_trade_history_lookback_days,
    get_trade_history_object_name,
    get_trade_history_source,
)
from market import CRYPTO_TARGETS, get_exchange

TRADE_COLUMNS = [
    "source",
    "timestamp",
    "symbol",
    "action",
    "amount",
    "price",
    "leverage",
    "stop_loss",
    "take_profit",
    "order_id",
    "trade_id",
]


def empty_trade_history() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)


@lru_cache(maxsize=1)
def get_storage_client() -> storage.Client:
    return storage.Client()


def _coerce_history_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    for column in TRADE_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[TRADE_COLUMNS]


def _deduplicate_history(df: pd.DataFrame) -> pd.DataFrame:
    frame = _coerce_history_frame(df).copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")

    with_order_id = frame[frame["order_id"].notna() & (frame["order_id"].astype(str).str.len() > 0)].drop_duplicates(
        subset=["order_id"], keep="first"
    )
    without_order_id = frame[~(frame["order_id"].notna() & (frame["order_id"].astype(str).str.len() > 0))]

    with_trade_id = without_order_id[
        without_order_id["trade_id"].notna() & (without_order_id["trade_id"].astype(str).str.len() > 0)
    ].drop_duplicates(subset=["trade_id"], keep="first")
    remainder = without_order_id[~(without_order_id["trade_id"].notna() & (without_order_id["trade_id"].astype(str).str.len() > 0))]

    if not remainder.empty:
        remainder = remainder.drop_duplicates(
            subset=["timestamp", "symbol", "action", "amount", "price", "leverage", "stop_loss", "take_profit"],
            keep="first",
        )

    merged = pd.concat([with_order_id, with_trade_id, remainder], ignore_index=True)
    merged = merged.sort_values(by="timestamp", ascending=False, na_position="last").reset_index(drop=True)
    return merged


def _persist_trade_mirror(df: pd.DataFrame) -> None:
    cleaned = _deduplicate_history(df)
    local_path = Path(get_trade_history_local_path())
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_csv(local_path, index=False)

    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        return

    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(get_trade_history_object_name())
    blob.upload_from_string(cleaned.to_csv(index=False), content_type="text/csv")


def _load_local_history() -> pd.DataFrame:
    local_path = Path(get_trade_history_local_path())
    if local_path.exists() and local_path.stat().st_size > 0:
        return _deduplicate_history(pd.read_csv(local_path))
    return empty_trade_history()


def _load_csv_bucket_history() -> pd.DataFrame:
    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        return _load_local_history()

    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(get_trade_history_object_name())

    try:
        payload = blob.download_as_text()
    except NotFound:
        return empty_trade_history()

    if not payload.strip():
        return empty_trade_history()

    return _deduplicate_history(pd.read_csv(StringIO(payload)))


def _load_api_history() -> pd.DataFrame:
    exchange = get_exchange()
    lookback_days = get_trade_history_lookback_days()
    limit = get_trade_history_limit()
    since_ms = int((datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days)).timestamp() * 1000)

    rows: list[dict] = []

    for symbol in CRYPTO_TARGETS:
        try:
            trades = exchange.fetch_my_trades(symbol=symbol, since=since_ms, limit=limit)
        except Exception:
            continue

        for trade in trades:
            rows.append(
                {
                    "source": "API",
                    "timestamp": pd.to_datetime(trade.get("timestamp"), unit="ms", utc=True).tz_convert(None)
                    if trade.get("timestamp") is not None
                    else pd.NaT,
                    "symbol": trade.get("symbol", symbol),
                    "action": str(trade.get("side", "")).upper(),
                    "amount": float(trade.get("amount") or trade.get("filled") or 0),
                    "price": float(trade.get("price") or 0),
                    "leverage": trade.get("leverage", 1),
                    "stop_loss": None,
                    "take_profit": None,
                    "order_id": trade.get("order") or trade.get("ordertxid"),
                    "trade_id": trade.get("id"),
                }
            )

    if not rows:
        return empty_trade_history()

    return _deduplicate_history(pd.DataFrame(rows))


def load_trade_history() -> pd.DataFrame:
    source = get_trade_history_source()

    if source in {"api", "hybrid", "auto"}:
        try:
            api_history = _load_api_history()
            local_history = _load_local_history()
            combined = pd.concat([api_history, local_history], ignore_index=True)
            merged = _deduplicate_history(combined)
            if not merged.empty:
                _persist_trade_mirror(merged)
                return merged
            if not api_history.empty:
                _persist_trade_mirror(api_history)
                return api_history
            if not local_history.empty:
                return local_history
        except Exception:
            if source == "api":
                return _load_local_history()
            if source in {"hybrid", "auto"}:
                local_history = _load_local_history()
                if not local_history.empty:
                    return local_history

    if source == "csv":
        return _load_csv_bucket_history()

    return _load_local_history()


def save_trade_history(df: pd.DataFrame) -> None:
    source = get_trade_history_source()
    cleaned = _deduplicate_history(df)

    if source in {"api", "hybrid", "auto"}:
        _persist_trade_mirror(cleaned)
        return

    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        local_path = Path(get_trade_history_local_path())
        local_path.parent.mkdir(parents=True, exist_ok=True)
        cleaned.to_csv(local_path, index=False)
        return

    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(get_trade_history_object_name())
    blob.upload_from_string(cleaned.to_csv(index=False), content_type="text/csv")


def ensure_trade_history() -> None:
    load_trade_history()


def append_trade_row(row: dict) -> None:
    df = load_trade_history()
    row = {"source": "LOCAL", **row}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    save_trade_history(df)


def has_recent_event(symbol: str, action: str, hours: int = 24) -> bool:
    df = load_trade_history()
    if df.empty:
        return False

    cutoff = pd.Timestamp.now() - pd.Timedelta(hours=hours)
    timestamps = pd.to_datetime(df["timestamp"], errors="coerce")
    mask = (
        df["symbol"].astype(str) == symbol
    ) & (
        df["action"].astype(str) == action
    ) & (
        timestamps >= cutoff
    )
    return bool(df[mask].shape[0])


def log_protection_rejection(symbol: str, reason: str, amount: float, price: float) -> None:
    append_trade_row(
        {
            "source": "LOCAL",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "action": "PROTECTION_REJECTED",
            "amount": amount,
            "price": price,
            "leverage": pd.NA,
            "stop_loss": pd.NA,
            "take_profit": pd.NA,
            "order_id": reason,
            "trade_id": pd.NA,
        }
    )
