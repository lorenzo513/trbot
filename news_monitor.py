from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import requests

from app_config import get_news_sentiment_local_path, get_news_sentiment_object_name, get_trade_history_bucket
from storage import get_storage_client

DEFAULT_NEWS_FEEDS = (
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/",
)

SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BTC/EUR": ("btc", "bitcoin", "xbt"),
    "SOL/EUR": ("sol", "solana"),
    "XRP/EUR": ("xrp", "ripple"),
    "LINK/EUR": ("link", "chainlink"),
    "ADA/EUR": ("ada", "cardano"),
    "DOGE/EUR": ("doge", "dogecoin"),
    "EDGE/EUR": ("edge",),
}

POSITIVE_WORDS = {
    "bullish",
    "surge",
    "surges",
    "rally",
    "rallies",
    "breakout",
    "approval",
    "approved",
    "partnership",
    "upgrade",
    "positive",
    "growth",
    "adoption",
    "gain",
    "gains",
    "record",
    "launch",
    "expansion",
}

NEGATIVE_WORDS = {
    "bearish",
    "drop",
    "drops",
    "dump",
    "plunge",
    "lawsuit",
    "investigation",
    "hack",
    "hackers",
    "rejection",
    "ban",
    "negative",
    "fear",
    "selloff",
    "sell-off",
    "crash",
    "crashes",
    "risk",
}


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    link: str
    published: str
    summary: str


def get_news_feed_urls() -> list[str]:
    raw_value = os.getenv("NEWS_FEEDS", "").strip()
    if not raw_value:
        return list(DEFAULT_NEWS_FEEDS)
    return [url.strip() for url in raw_value.split(",") if url.strip()]


def get_news_monitor_enabled() -> bool:
    value = os.getenv("NEWS_MONITOR_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_news_block_buys_enabled() -> bool:
    value = os.getenv("NEWS_BLOCK_BUYS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_news_negative_threshold() -> float:
    raw_value = os.getenv("NEWS_NEGATIVE_THRESHOLD", "-0.35")
    try:
        return float(raw_value)
    except ValueError:
        return -0.35


def get_ml_sentiment_enabled() -> bool:
    value = os.getenv("NEWS_ML_SENTIMENT_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_news_feed_cache_seconds() -> int:
    raw_value = os.getenv("NEWS_FEED_CACHE_SECONDS", "600")
    try:
        return max(60, int(raw_value))
    except ValueError:
        return 600


def get_news_sentiment_cache_seconds() -> int:
    raw_value = os.getenv("NEWS_SENTIMENT_CACHE_SECONDS", "86400")
    try:
        return max(300, int(raw_value))
    except ValueError:
        return 86400


_feed_cache: tuple[float, dict[str, list[ET.Element]]] | None = None
_sentiment_cache: dict[str, tuple[float, dict[str, object]]] = {}
_persisted_cache_loaded = False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _tag_name(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def _first_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if _tag_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _first_link(node: ET.Element) -> str:
    for child in list(node):
        if _tag_name(child.tag) == "link":
            href = child.attrib.get("href", "").strip()
            if href:
                return href
            if child.text:
                return child.text.strip()
    return ""


def _iter_feed_items(feed_xml: str) -> list[ET.Element]:
    root = ET.fromstring(feed_xml)
    items = []
    for element in root.iter():
        if _tag_name(element.tag) in {"item", "entry"}:
            items.append(element)
    return items


def _keyword_matches(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = _normalize_text(text)
    return any(keyword in normalized for keyword in keywords)


def _symbol_keywords(symbol: str) -> tuple[str, ...]:
    return SYMBOL_KEYWORDS.get(symbol, (symbol.split("/")[0].lower(),))


@lru_cache(maxsize=1)
def _load_sentiment_pipeline():
    if not get_ml_sentiment_enabled():
        return None

    model_name = os.getenv("NEWS_SENTIMENT_MODEL", "ProsusAI/finbert")
    try:
        from transformers import pipeline
    except Exception:
        return None

    try:
        model = pipeline("sentiment-analysis", model=model_name)
        print(f"[News] Modello ML sentiment caricato: {model_name}")
        return model
    except Exception:
        return None


def _lexicon_sentiment_score(text: str) -> float:
    normalized = _normalize_text(text)
    positive_hits = sum(1 for word in POSITIVE_WORDS if word in normalized)
    negative_hits = sum(1 for word in NEGATIVE_WORDS if word in normalized)
    total_hits = positive_hits + negative_hits
    if total_hits == 0:
        return 0.0
    return (positive_hits - negative_hits) / total_hits


def score_sentiment(text: str) -> tuple[str, float]:
    cleaned = text.strip()
    if not cleaned:
        return "NEUTRAL", 0.0

    sentiment_pipeline = _load_sentiment_pipeline()
    if sentiment_pipeline is not None:
        try:
            result = sentiment_pipeline(cleaned[:512])[0]
            label = str(result.get("label", "NEUTRAL")).upper()
            score = float(result.get("score", 0.0))
            if "NEG" in label:
                return "NEGATIVE", -score
            if "POS" in label:
                return "POSITIVE", score
            return "NEUTRAL", 0.0
        except Exception:
            pass

    score = _lexicon_sentiment_score(cleaned)
    if score > 0.1:
        return "POSITIVE", score
    if score < -0.1:
        return "NEGATIVE", score
    return "NEUTRAL", score


def _load_cached_feed_items(timeout: int = 10) -> dict[str, list[ET.Element]]:
    global _feed_cache

    now = time.time()
    cache_ttl = get_news_feed_cache_seconds()
    if _feed_cache and now - _feed_cache[0] < cache_ttl:
        return _feed_cache[1]

    feeds: dict[str, list[ET.Element]] = {}
    for feed_url in get_news_feed_urls():
        try:
            response = requests.get(feed_url, timeout=timeout)
            response.raise_for_status()
            feeds[feed_url] = _iter_feed_items(response.text)
        except Exception:
            continue

    if feeds:
        _feed_cache = (now, feeds)
    elif _feed_cache:
        return _feed_cache[1]

    return feeds


def fetch_news_items(symbol: str, limit: int = 8, timeout: int = 10) -> list[NewsItem]:
    if not get_news_monitor_enabled():
        return []

    keywords = _symbol_keywords(symbol)
    items: list[NewsItem] = []

    for feed_url, feed_items in _load_cached_feed_items(timeout=timeout).items():
        for entry in feed_items:
            title = _first_text(entry, ("title",))
            summary = _first_text(entry, ("description", "summary", "content"))
            published = _first_text(entry, ("pubdate", "published", "updated"))
            link = _first_link(entry)
            haystack = f"{title} {summary}"

            if not title:
                continue
            if not _keyword_matches(haystack, keywords):
                continue

            items.append(
                NewsItem(
                    source=feed_url,
                    title=title,
                    link=link,
                    published=published,
                    summary=summary,
                )
            )

            if len(items) >= limit:
                return items

    return items


def _format_cache_age(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return "pochi secondi fa"

    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} min fa"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h fa"

    days = hours // 24
    return f"{days}g fa"


def format_news_cache_age(seconds: float) -> str:
    return _format_cache_age(seconds)


def _news_item_to_dict(item: NewsItem) -> dict[str, str]:
    return asdict(item)


def _news_item_from_dict(data: dict[str, object]) -> NewsItem:
    return NewsItem(
        source=str(data.get("source", "")),
        title=str(data.get("title", "")),
        link=str(data.get("link", "")),
        published=str(data.get("published", "")),
        summary=str(data.get("summary", "")),
    )


def _result_from_persisted_entry(symbol: str, entry: dict[str, object]) -> dict[str, object]:
    raw_items = entry.get("items", [])
    items = [_news_item_from_dict(item) for item in raw_items] if isinstance(raw_items, list) else []
    return {
        "symbol": symbol,
        "label": str(entry.get("label", "NO_DATA")),
        "score": float(entry.get("score", 0.0)),
        "items": items,
        "cached_at": float(entry.get("cached_at", 0.0)),
        "from_cache": False,
        "cache_age_seconds": 0.0,
        "cache_source": "persisted",
    }


def _result_to_persisted_entry(cached_at: float, result: dict[str, object]) -> dict[str, object]:
    items = result.get("items", [])
    serialized_items = [_news_item_to_dict(item) for item in items] if isinstance(items, list) else []
    return {
        "cached_at": cached_at,
        "label": result.get("label", "NO_DATA"),
        "score": float(result.get("score", 0.0)),
        "items": serialized_items,
    }


def _load_local_sentiment_document() -> dict[str, object]:
    local_path = Path(get_news_sentiment_local_path())
    if not local_path.exists() or local_path.stat().st_size == 0:
        return {"version": 1, "symbols": {}}

    try:
        payload = json.loads(local_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "symbols": {}}

    if not isinstance(payload, dict):
        return {"version": 1, "symbols": {}}
    payload.setdefault("symbols", {})
    return payload


def _load_gcs_sentiment_document() -> dict[str, object] | None:
    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        return None

    try:
        from google.api_core.exceptions import NotFound

        client = get_storage_client()
        blob = client.bucket(bucket_name).blob(get_news_sentiment_object_name())
        payload = blob.download_as_text(encoding="utf-8")
    except NotFound:
        return None
    except Exception as exc:
        print(f"[News] Impossibile leggere la cache sentiment da GCS: {exc}")
        return None

    if not payload.strip():
        return None

    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        print("[News] Cache sentiment GCS non valida, verra rigenerata.")
        return None

    if not isinstance(document, dict):
        return None
    document.setdefault("symbols", {})
    return document


def _merge_sentiment_documents(*documents: dict[str, object]) -> dict[str, tuple[float, dict[str, object]]]:
    merged: dict[str, tuple[float, dict[str, object]]] = {}

    for document in documents:
        symbols = document.get("symbols", {})
        if not isinstance(symbols, dict):
            continue

        for symbol, entry in symbols.items():
            if not isinstance(entry, dict):
                continue
            cached_at = float(entry.get("cached_at", 0.0))
            current = merged.get(symbol)
            if current is None or cached_at > current[0]:
                merged[symbol] = (cached_at, _result_from_persisted_entry(symbol, entry))

    return merged


def _save_sentiment_document() -> None:
    symbols = {
        symbol: _result_to_persisted_entry(cached_at, result)
        for symbol, (cached_at, result) in _sentiment_cache.items()
    }
    document = {
        "version": 1,
        "updated_at": time.time(),
        "symbols": symbols,
    }
    payload = json.dumps(document, ensure_ascii=False, indent=2)

    local_path = Path(get_news_sentiment_local_path())
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(payload, encoding="utf-8")

    bucket_name = get_trade_history_bucket()
    if not bucket_name:
        return

    try:
        client = get_storage_client()
        blob = client.bucket(bucket_name).blob(get_news_sentiment_object_name())
        blob.upload_from_string(payload, content_type="application/json")
        print(f"[News] Cache sentiment salvata su GCS ({len(symbols)} symbol).")
    except Exception as exc:
        print(f"[News] Impossibile salvare la cache sentiment su GCS: {exc}")


def _ensure_sentiment_cache_loaded() -> None:
    global _persisted_cache_loaded

    if _persisted_cache_loaded:
        return

    _persisted_cache_loaded = True
    documents = [_load_local_sentiment_document()]
    gcs_document = _load_gcs_sentiment_document()
    if gcs_document is not None:
        documents.append(gcs_document)

    merged = _merge_sentiment_documents(*documents)
    if not merged:
        return

    _sentiment_cache.update(merged)
    sources = ["locale"]
    if gcs_document is not None:
        sources.append("GCS")
    print(f"[News] Cache sentiment caricata da {' + '.join(sources)}: {len(merged)} symbol.")


def _build_cache_response(
    result: dict[str, object],
    cached_at: float,
    now: float,
    cache_source: str,
) -> dict[str, object]:
    age_seconds = now - cached_at
    response = dict(result)
    response["from_cache"] = True
    response["cache_age_seconds"] = age_seconds
    response["cache_source"] = cache_source
    print(
        f"[News] Sentiment {response['symbol']} da cache {cache_source} ({_format_cache_age(age_seconds)}): "
        f"{response['label']} ({float(response['score']):.2f})"
    )
    return response


def analyze_symbol_news(symbol: str, limit: int = 8, *, persist: bool = True) -> dict[str, object]:
    _ensure_sentiment_cache_loaded()

    now = time.time()
    cache_ttl = get_news_sentiment_cache_seconds()
    cached = _sentiment_cache.get(symbol)
    if cached and now - cached[0] < cache_ttl:
        cache_source = str(cached[1].get("cache_source", "RAM"))
        return _build_cache_response(cached[1], cached[0], now, cache_source)

    items = fetch_news_items(symbol, limit=limit)
    combined_text = " ".join(f"{item.title} {item.summary}" for item in items)
    label, score = score_sentiment(combined_text)

    if not items:
        result = {
            "symbol": symbol,
            "label": "NO_DATA",
            "score": 0.0,
            "items": [],
        }
    else:
        result = {
            "symbol": symbol,
            "label": label,
            "score": score,
            "items": items,
        }

    result["cached_at"] = now
    result["from_cache"] = False
    result["cache_age_seconds"] = 0.0
    result["cache_source"] = "computed"
    print(f"[News] Sentiment {symbol} calcolato: {result['label']} ({float(result['score']):.2f})")

    _sentiment_cache[symbol] = (now, result)
    if persist:
        _save_sentiment_document()
    return result


def flush_sentiment_cache() -> None:
    if _sentiment_cache:
        _save_sentiment_document()
