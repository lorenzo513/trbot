from __future__ import annotations

import datetime
import gc
import json
import time
from pathlib import Path
from typing import Any

from app_config import get_dashboard_snapshot_local_path, get_dashboard_snapshot_object_name, get_trade_history_bucket
from market import (
    CORE_CRYPTO_TARGETS,
    LEVERAGE,
    MAX_CONCURRENT_TRADES,
    TIMEFRAME,
    TRADE_AMOUNT_EUR,
    compute_trade_signal,
    get_all_candidate_symbols,
    get_balance_snapshot,
    get_market_data,
    get_open_position_counts,
    get_open_positions,
    get_volatility_pct,
    is_dynamic_crypto_enabled,
    is_positive_trend,
)
from news_monitor import (
    analyze_symbol_news,
    flush_sentiment_cache,
    format_news_cache_age,
    get_news_block_buys_enabled,
    get_news_monitor_enabled,
)
from storage import get_storage_client

_SYMBOL_ERROR_TEMPLATE = {
    "last_price": None,
    "rsi": None,
    "ema_9": None,
    "trend": "N/A",
    "signal": "N/A",
    "sentiment": "ERROR",
    "score": 0.0,
    "sentiment_age": "N/A",
    "news_items": 0,
    "top_headline": "N/A",
    "volatility_pct": 0.0,
}


def _empty_snapshot() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        "updated_at_unix": 0.0,
        "config": {},
        "balance": {
            "free_eur": 0.0,
            "used_eur": 0.0,
            "total_eur": 0.0,
        },
        "positions": {
            "open_positions": [],
            "open_position_counts": {},
        },
        "candidate_symbols": [],
        "symbols": {},
    }


def _sentiment_age_label(news_snapshot: dict[str, object]) -> str:
    if news_snapshot.get("from_cache"):
        cache_age_seconds = float(news_snapshot.get("cache_age_seconds", 0.0))
        cache_source = str(news_snapshot.get("cache_source", "cache"))
        return f"{format_news_cache_age(cache_age_seconds)} ({cache_source})"
    return "appena calcolato"


def _build_symbol_snapshot(symbol: str, open_position_counts: dict[str, int]) -> dict[str, Any]:
    market_df = get_market_data(symbol)
    last_row = market_df.iloc[-1]
    current_price = float(last_row["close"])
    rsi = float(last_row["RSI"])
    ema_9 = float(last_row["EMA_9"])
    news_snapshot = analyze_symbol_news(symbol, persist=False)
    news_label = str(news_snapshot["label"])
    news_score = float(news_snapshot["score"])
    news_items_count = int(news_snapshot.get("news_items_count", 0))
    top_headline = str(news_snapshot.get("top_headline", "N/A"))
    trend_positive = is_positive_trend(market_df)

    return {
        "core": symbol in CORE_CRYPTO_TARGETS,
        "last_price": round(current_price, 4),
        "rsi": round(rsi, 2),
        "ema_9": round(ema_9, 4),
        "trend": "UP" if trend_positive else "DOWN",
        "signal": compute_trade_signal(rsi, current_price, ema_9, trend_positive, news_label),
        "sentiment": news_label,
        "score": round(news_score, 2),
        "sentiment_age": _sentiment_age_label(news_snapshot),
        "news_items": news_items_count,
        "top_headline": top_headline,
        "volatility_pct": round(get_volatility_pct(market_df), 2),
        "open_trades": open_position_counts.get(symbol, 0),
    }


def _error_symbol_snapshot(symbol: str, exc: Exception, open_position_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "core": symbol in CORE_CRYPTO_TARGETS,
        "signal": f"Error: {exc}",
        "top_headline": str(exc),
        "open_trades": open_position_counts.get(symbol, 0),
        **_SYMBOL_ERROR_TEMPLATE,
    }


def build_dashboard_snapshot() -> dict[str, Any]:
    candidate_symbols = get_all_candidate_symbols()
    open_positions = get_open_positions()
    open_position_counts = get_open_position_counts(open_positions)
    balance_snapshot = get_balance_snapshot()

    symbols: dict[str, Any] = {}
    for symbol in candidate_symbols:
        try:
            symbols[symbol] = _build_symbol_snapshot(symbol, open_position_counts)
        except Exception as exc:
            symbols[symbol] = _error_symbol_snapshot(symbol, exc, open_position_counts)

        # Ripulitura della memoria dopo ogni simbolo processato
        gc.collect()

    flush_sentiment_cache()

    now = datetime.datetime.now()
    return {
        "version": 1,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_unix": time.time(),
        "config": {
            "timeframe": TIMEFRAME,
            "trade_amount_eur": TRADE_AMOUNT_EUR,
            "leverage": LEVERAGE,
            "max_concurrent_trades": MAX_CONCURRENT_TRADES,
            "dynamic_crypto_enabled": is_dynamic_crypto_enabled(),
            "news_monitor_enabled": get_news_monitor_enabled(),
            "news_block_buys": get_news_block_buys_enabled(),
        },
        "balance": balance_snapshot,
        "positions": {
            "open_positions": open_positions,
            "open_position_counts": open_position_counts,
        },
        "candidate_symbols": candidate_symbols,
        "symbols": symbols,
    }


def _load_local_snapshot() -> dict[str, Any] | None:
    local_path = Path(get_dashboard_snapshot_local_path())
    if not local_path.exists() or local_path.stat().st_size == 0:
        return None

    try:
        payload = json.loads(local_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    return payload if isinstance(payload, dict) else None


def _load_gcs_snapshot() -> dict[str, Any] | None:
    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        return None

    try:
        from google.api_core.exceptions import NotFound

        client = get_storage_client()
        blob = client.bucket(bucket_name).blob(get_dashboard_snapshot_object_name())
        payload = blob.download_as_text(encoding="utf-8")
    except NotFound:
        return None
    except Exception as exc:
        print(f"[Dashboard] Impossibile leggere lo snapshot da GCS: {exc}")
        return None

    if not payload.strip():
        return None

    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        print("[Dashboard] Snapshot GCS non valido.")
        return None

    return document if isinstance(document, dict) else None


def save_dashboard_snapshot(snapshot: dict[str, Any]) -> None:
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)

    local_path = Path(get_dashboard_snapshot_local_path())
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(payload, encoding="utf-8")

    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        print("[Dashboard] Snapshot salvato in locale.")
        return

    try:
        client = get_storage_client()
        blob = client.bucket(bucket_name).blob(get_dashboard_snapshot_object_name())
        blob.upload_from_string(payload, content_type="application/json")
        print(f"[Dashboard] Snapshot salvato su GCS ({len(snapshot.get('symbols', {}))} symbol).")
    except Exception as exc:
        print(f"[Dashboard] Impossibile salvare lo snapshot su GCS: {exc}")


def load_dashboard_snapshot() -> dict[str, Any]:
    gcs_snapshot = _load_gcs_snapshot()
    local_snapshot = _load_local_snapshot()

    candidates = [item for item in (gcs_snapshot, local_snapshot) if item is not None]
    if not candidates:
        return _empty_snapshot()

    def _updated_at_unix(document: dict[str, Any]) -> float:
        try:
            return float(document.get("updated_at_unix", 0.0))
        except (TypeError, ValueError):
            return 0.0

    return max(candidates, key=_updated_at_unix)


def publish_dashboard_snapshot() -> dict[str, Any]:
    snapshot = build_dashboard_snapshot()
    save_dashboard_snapshot(snapshot)
    return snapshot