"""
avito_parser.py

Core logic for the HONEX mini test task: search a list of article numbers
on Avito, keep only *new* condition items from Moscow / Moscow region,
and return the 5 cheapest matching listings per article.

The module is split into three layers so each part can be tested/replaced
independently:

    build_search_url()   -- turns an article into an Avito search URL
    fetch_html()          -- does the HTTP request (network layer)
    parse_search_page()   -- turns raw HTML into a list of Listing objects

`search_article()` wires the three layers together and additionally
supports reading HTML from a local file instead of the network, which is
what the task statement explicitly allows if Avito blocks the request
directly ("покажите формирование поисковых запросов и обработку
предоставленных / собственных тестовых HTML-файлов").
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("avito_parser")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://www.avito.ru/moskovskaya_oblast"  # Москва и МО в одном разделе
REQUEST_TIMEOUT = 15  # seconds
MAX_RETRIES = 2
RETRY_DELAY = 2  # seconds
TOP_N = 5  # сколько самых дешёвых объявлений сохранять по артикулу

# Query params used to scope the search on Avito's side. These were taken
# from manually inspecting avito.ru search URLs; Avito changes its markup
# and params occasionally, so if a live run stops matching, this is the
# first place to check.
SEARCH_PARAMS = {
    "state": "new",   # состояние товара: только новое
    "s": "104",        # сортировка по возрастанию цены
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Regions (or substrings thereof) that count as "Moscow / Moscow region".
# Avito shows the district/city text for every listing, e.g.
# "Москва, м. Свиблово" or just "Балашиха" (Avito rarely repeats
# "Московская область" on every card, it shows the town name instead).
# The search URL (BASE_URL) already scopes results to the Moscow/MO
# section server-side, so this list is a secondary, defense-in-depth
# check -- it is not meant to be an exhaustive gazetteer of every town in
# the region, just the most common ones plus Moscow itself. Extend it if
# a real run turns up a legitimate MO town that isn't in the list yet.
ALLOWED_REGION_MARKERS = (
    "москва",
    "московская обл",
    "моск. обл",
    "балашиха",
    "химки",
    "подольск",
    "королёв",
    "королев",
    "мытищи",
    "люберцы",
    "электросталь",
    "коломна",
    "одинцово",
    "домодедово",
    "серпухов",
    "орехово-зуево",
    "раменское",
    "жуковский",
    "пушкино",
    "ивантеевка",
    "дзержинский",
    "ногинск",
    "реутов",
    "долгопрудный",
    "щёлково",
    "щелково",
    "сергиев посад",
    "красногорск",
    "видное",
    "наро-фоминск",
    "чехов",
    "клин",
    "егорьевск",
    "воскресенск",
    "павловский посад",
    "кашира",
    "лобня",
    "фрязино",
    "лыткарино",
    "звенигород",
    "апрелевка",
    "талдом",
)


@dataclass
class Listing:
    article: str
    query: str
    title: str
    price: Optional[int]
    region: str
    condition: str
    url: str
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    rank: Optional[int] = None
    status: str = "ok"  # ok | не найдено | ошибка


def build_search_url(query: str) -> str:
    """Build an Avito search URL for the given free-text query."""
    params = {"q": query, **SEARCH_PARAMS}
    return f"{BASE_URL}?{urlencode(params)}"


def fetch_html(url: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """
    Fetch a page's HTML. Returns None (rather than raising) on any network
    or HTTP-level failure so the caller can record a per-article error row
    instead of crashing the whole run.
    """
    sess = session or requests
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = sess.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            logger.warning("HTTP %s for %s (attempt %s/%s)", resp.status_code, url, attempt, MAX_RETRIES)
            if resp.status_code in (403, 429):
                # Likely bot-blocked (captcha / rate limit) — retrying won't help much,
                # but we still allow one retry in case it was a transient block.
                last_exc = RuntimeError(f"blocked, status={resp.status_code}")
            else:
                last_exc = RuntimeError(f"unexpected status={resp.status_code}")
        except requests.RequestException as exc:
            logger.warning("Request error for %s: %s (attempt %s/%s)", url, exc, attempt, MAX_RETRIES)
            last_exc = exc
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    logger.error("Giving up on %s: %s", url, last_exc)
    return None


def _clean_price(raw: str) -> Optional[int]:
    """Extract an integer price (rubles) from a raw price string, or None."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def _is_allowed_region(region_text: str) -> bool:
    text = (region_text or "").lower()
    return any(marker in text for marker in ALLOWED_REGION_MARKERS)


def parse_search_page(html: str, article: str, query: str) -> list[Listing]:
    """
    Parse an Avito search results page into a list of Listing objects.

    Avito's markup uses `data-marker` attributes on the relevant elements;
    these have stayed comparatively stable across redesigns and are the
    same hooks used in most public Avito-scraper writeups, so we key off
    them with a couple of class-based fallbacks in case a given attribute
    is missing on some page variant.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-marker="item"]')

    listings: list[Listing] = []
    for card in cards:
        try:
            title_el = card.select_one('[data-marker="item-title"]') or card.select_one("a[title]")
            if not title_el:
                continue
            title = title_el.get("title") or title_el.get_text(strip=True)
            href = title_el.get("href", "")
            url = href if href.startswith("http") else f"https://www.avito.ru{href}"

            price_el = card.select_one('[data-marker="item-price"]') or card.select_one(
                'meta[itemprop="price"]'
            )
            raw_price = (
                price_el.get("content")
                if price_el and price_el.has_attr("content")
                else (price_el.get_text(strip=True) if price_el else "")
            )
            price = _clean_price(raw_price)

            geo_el = card.select_one('[data-marker="item-address"]') or card.select_one(
                '[class*="geo-address"]'
            )
            region = geo_el.get_text(" ", strip=True) if geo_el else ""

            # Condition isn't always rendered on the card itself (it's the
            # `state=new` search filter that mainly enforces this), but
            # some cards do carry a "Новое"/"Б/у" badge — use it as a
            # second check when present, otherwise trust the query filter.
            condition_el = card.select_one('[data-marker="item-condition"]')
            condition_text = condition_el.get_text(strip=True) if condition_el else "новое (по фильтру)"

            listings.append(
                Listing(
                    article=article,
                    query=query,
                    title=title,
                    price=price,
                    region=region,
                    condition=condition_text,
                    url=url,
                )
            )
        except Exception:
            logger.exception("Failed to parse one listing card, skipping it")
            continue

    return listings


def filter_and_rank(listings: list[Listing]) -> list[Listing]:
    """
    Apply the business rules from the task:
      - drop items whose condition text explicitly says "б/у" (not new)
      - drop items outside Moscow / Moscow region
      - drop duplicate URLs (repeat listings) within the same article
      - drop items with no numeric price (they can't be price-ranked)
      - sort by price ascending and keep only the TOP_N cheapest
      - assign a 1-based rank
    """
    seen_urls: set[str] = set()
    kept: list[Listing] = []

    for item in listings:
        if item.price is None:
            continue
        if "б/у" in item.condition.lower() or "бу" == item.condition.lower().strip():
            continue
        if not _is_allowed_region(item.region):
            continue
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        kept.append(item)

    kept.sort(key=lambda x: x.price)
    kept = kept[:TOP_N]
    for i, item in enumerate(kept, start=1):
        item.rank = i
    return kept


def search_article(
    article: str,
    description: str,
    session: Optional[requests.Session] = None,
    html_override: Optional[str] = None,
) -> list[Listing]:
    """
    Run the full pipeline for one article: build the query, get the HTML
    (either live from Avito, or from `html_override` for offline/testing
    use), parse it, filter it, and return up to TOP_N Listing rows.

    If nothing could be fetched, or nothing matched, a single Listing with
    status "ошибка"/"не найдено" is returned instead, per the task spec.
    """
    query = f"{article} {description}".strip()
    url = build_search_url(query)

    if html_override is not None:
        html = html_override
    else:
        html = fetch_html(url, session=session)

    if html is None:
        return [
            Listing(
                article=article,
                query=query,
                title="",
                price=None,
                region="",
                condition="",
                url=url,
                status="ошибка",
            )
        ]

    raw_listings = parse_search_page(html, article, query)
    result = filter_and_rank(raw_listings)

    if not result:
        return [
            Listing(
                article=article,
                query=query,
                title="",
                price=None,
                region="",
                condition="",
                url=url,
                status="не найдено",
            )
        ]

    return result
