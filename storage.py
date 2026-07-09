from functools import lru_cache
from io import StringIO
from pathlib import Path

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import storage

from app_config import (
    get_trade_history_bucket,
    get_trade_history_local_path,
    get_trade_history_object_name,
)

TRADE_COLUMNS = [
    "timestamp",
    "symbol",
    "action",
    "amount",
    "price",
    "leverage",
    "stop_loss",
    "take_profit",
]


def empty_trade_history() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)


@lru_cache(maxsize=1)
def get_storage_client() -> storage.Client:
    return storage.Client()


def _load_local_history() -> pd.DataFrame:
    local_path = Path(get_trade_history_local_path())
    if local_path.exists() and local_path.stat().st_size > 0:
        return pd.read_csv(local_path)
    return empty_trade_history()


def _save_local_history(df: pd.DataFrame) -> None:
    local_path = Path(get_trade_history_local_path())
    local_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(local_path, index=False)


def load_trade_history() -> pd.DataFrame:
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

    return pd.read_csv(StringIO(payload))


def save_trade_history(df: pd.DataFrame) -> None:
    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        _save_local_history(df)
        return

    client = get_storage_client()
    blob = client.bucket(bucket_name).blob(get_trade_history_object_name())
    blob.upload_from_string(df.to_csv(index=False), content_type="text/csv")


def ensure_trade_history() -> None:
    if load_trade_history().empty:
        save_trade_history(empty_trade_history())


def append_trade_row(row: dict) -> None:
    df = load_trade_history()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    save_trade_history(df)
