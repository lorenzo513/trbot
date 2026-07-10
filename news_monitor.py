from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import requests

DEFAULT_NEWS_FEEDS = (
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/",
)

SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
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
    "lawsuit",
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
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_news_negative_threshold() -> float:
    raw_value = os.getenv("NEWS_NEGATIVE_THRESHOLD", "-0.35")
    try:
        return float(raw_value)
    except ValueError:
        return -0.35


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
    model_name = os.getenv("NEWS_SENTIMENT_MODEL", "ProsusAI/finbert")
    try:
        from transformers import pipeline
    except Exception:
        return None

    try:
        return pipeline("sentiment-analysis", model=model_name)
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


def fetch_news_items(symbol: str, limit: int = 8, timeout: int = 10) -> list[NewsItem]:
    if not get_news_monitor_enabled():
        return []

    keywords = _symbol_keywords(symbol)
    items: list[NewsItem] = []

    for feed_url in get_news_feed_urls():
        try:
            response = requests.get(feed_url, timeout=timeout)
            response.raise_for_status()
            feed_items = _iter_feed_items(response.text)
        except Exception:
            continue

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


def analyze_symbol_news(symbol: str, limit: int = 8) -> dict[str, object]:
    items = fetch_news_items(symbol, limit=limit)
    combined_text = " ".join(f"{item.title} {item.summary}" for item in items)
    label, score = score_sentiment(combined_text)

    if not items:
        return {
            "symbol": symbol,
            "label": "NO_DATA",
            "score": 0.0,
            "items": [],
        }

    return {
        "symbol": symbol,
        "label": label,
        "score": score,
        "items": items,
    }
