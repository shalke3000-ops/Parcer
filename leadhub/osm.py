"""Local-business search: Nominatim geocoding + Overpass API.

Only fixed, whitelisted tag filters and numeric coordinates are interpolated
into the Overpass query, so user input can never alter the query language.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .db import BusinessRecord, Database

log = logging.getLogger(__name__)
ALLOWED_RADII_KM = (1, 3, 5, 10)
NOMINATIM_MIN_INTERVAL_S = 1.1  # Nominatim usage policy: max 1 request/second
OVERPASS_TIMEOUT_S = 60  # shared public servers can queue a query for most of a minute
HTTP_TIMEOUT_S = OVERPASS_TIMEOUT_S + 15.0
MAX_RESULTS = 4000  # the centre of a mid-size city alone exceeds 1500 named places
OVERPASS_RETRY_DELAY_S = 0.5
METRES_PER_DEGREE = 111_320
MIN_LONGITUDE_SCALE = 0.01  # keeps the box finite next to the poles


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    filters: tuple[tuple[str, str], ...]  # (osm key, regex over values)
    default: bool = False


CATEGORIES: tuple[Category, ...] = (
    Category("restaurant", "Рестораны", (("amenity", "restaurant"),), True),
    Category("cafe", "Кофейни и кафе", (("amenity", "cafe"),), True),
    Category("fast_food", "Фастфуд", (("amenity", "fast_food"),)),
    Category("bar", "Бары и пабы", (("amenity", "bar|pub"),)),
    Category("barber", "Барбершопы и парикмахерские", (("shop", "hairdresser"),), True),
    Category("beauty", "Салоны красоты, ногти", (("shop", "beauty|cosmetics"),), True),
    Category("bakery", "Пекарни и кондитерские", (("shop", "bakery|pastry|confectionery"),)),
    Category("retail", "Розница: одежда, цветы, подарки", (("shop", "clothes|shoes|florist|gift|jewelry|toys"),), True),
    Category("fitness", "Фитнес и студии", (("leisure", "fitness_centre|sports_centre"),)),
    Category("auto", "Автосервис и мойки", (("shop", "car_repair|tyres"), ("amenity", "car_wash"))),
    Category("dentist", "Стоматологии", (("amenity", "dentist"),)),
    Category("lodging", "Гостиницы и хостелы", (("tourism", "hotel|guest_house|hostel"),)),
)
CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

WEBSITE_KEYS = ("website", "contact:website", "url", "contact:url")
MESSENGER_KEYS = (
    "contact:telegram", "telegram", "contact:whatsapp", "whatsapp", "contact:viber", "viber",
    "contact:signal", "contact:line", "contact:wechat",
)
SOCIAL_KEYS = ("contact:instagram", "instagram", "contact:vk", "vk", "contact:facebook", "facebook")
PHONE_KEYS = ("phone", "contact:phone", "contact:mobile", "mobile")
CHAIN_KEYS = ("brand", "brand:wikidata")  # a chain branch has a corporate site even when OSM omits it


class OsmError(Exception):
    """User-presentable failure from Nominatim/Overpass."""


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    label: str


_last_nominatim_call = 0.0
_nominatim_lock = asyncio.Lock()


def bounding_box(lat: float, lon: float, radius_m: int) -> tuple[float, float, float, float]:
    """South, west, north, east of the square around a point, clamped to valid coordinates."""
    lat_span = radius_m / METRES_PER_DEGREE
    lon_span = lat_span / max(math.cos(math.radians(lat)), MIN_LONGITUDE_SCALE)
    return max(lat - lat_span, -90.0), max(lon - lon_span, -180.0), min(lat + lat_span, 90.0), min(lon + lon_span, 180.0)


def build_overpass_query(lat: float, lon: float, radius_m: int, categories: tuple[Category, ...]) -> str:
    """One statement per OSM key inside a bounding box: far cheaper than a radius scan per category.

    The box is a superset of the circle; search_businesses enforces the real radius afterwards.
    """
    values_by_key: dict[str, list[str]] = {}
    for category in categories:
        for key, values in category.filters:
            values_by_key.setdefault(key, []).append(values)
    body = "\n".join(f'  nwr["{key}"~"^({"|".join(values)})$"]["name"];' for key, values in values_by_key.items())
    south, west, north, east = bounding_box(lat, lon, radius_m)
    settings = f"[out:json][timeout:{OVERPASS_TIMEOUT_S}][bbox:{south:.6f},{west:.6f},{north:.6f},{east:.6f}]"
    return f"{settings};\n(\n{body}\n);\nout tags center {MAX_RESULTS};"


def classify(tags: dict[str, str], categories: tuple[Category, ...]) -> Category | None:
    for category in categories:
        for key, values in category.filters:
            if key in tags and re.fullmatch(values, tags[key]):
                return category
    return None


def _first(tags: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = tags.get(key, "").strip()
        if value:
            return value
    return ""


def normalize_website(value: str) -> str:
    """Return an http(s) URL or '' — never a javascript:/data: or other scheme."""
    value = value.strip().split(";")[0].strip()
    if not value:
        return ""
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", value, re.IGNORECASE):
        return f"https://{value}"
    return ""


def format_address(tags: dict[str, str]) -> str:
    street = " ".join(p for p in (tags.get("addr:street", ""), tags.get("addr:housenumber", "")) if p)
    parts = [street, tags.get("addr:city", "")]
    return ", ".join(p for p in parts if p) or tags.get("addr:full", "")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def element_to_business(
    element: dict[str, Any], center: GeoPoint, city: str, categories: tuple[Category, ...]
) -> BusinessRecord | None:
    tags = {str(k): str(v) for k, v in (element.get("tags") or {}).items()}
    name = tags.get("name", "").strip()
    category = classify(tags, categories)
    lat = element.get("lat", (element.get("center") or {}).get("lat"))
    lon = element.get("lon", (element.get("center") or {}).get("lon"))
    if not name or category is None or lat is None or lon is None:
        return None

    raw_website = _first(tags, WEBSITE_KEYS)
    messenger = _first(tags, MESSENGER_KEYS)
    return BusinessRecord(
        osm_type=str(element.get("type", "node")),
        osm_id=int(element["id"]),
        name=name[:200],
        category=category.key,
        lat=float(lat),
        lon=float(lon),
        address=format_address(tags)[:300],
        phone=_first(tags, PHONE_KEYS)[:100],
        website=normalize_website(raw_website),
        messenger=messenger[:200],
        social=_first(tags, SOCIAL_KEYS)[:200],
        # A website tag that is present but unparseable still counts as "has a website".
        opportunity=not raw_website and not messenger and not _first(tags, CHAIN_KEYS),
        city=city,
        distance_m=haversine_m(center.lat, center.lon, float(lat), float(lon)),
        tags=tags,
    )


async def geocode(client: httpx.AsyncClient, settings: Settings, db: Database, city: str) -> GeoPoint:
    global _last_nominatim_call
    cache_key = city.strip().lower()
    cached = db.cached_geocode(cache_key)
    if cached:
        return GeoPoint(cached["lat"], cached["lon"], cached["label"])

    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _last_nominatim_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_nominatim_call = time.monotonic()
        try:
            response = await client.get(
                settings.nominatim_url,
                params={"q": city, "format": "jsonv2", "limit": 1},  # no language: places keep their local names
                headers={"User-Agent": settings.osm_user_agent},
            )
            response.raise_for_status()
            results = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OsmError("Сервис геокодирования (Nominatim) недоступен, попробуйте позже.") from exc

    if not results:
        raise OsmError(f"Город «{city}» не найден в OpenStreetMap.")
    top = results[0]
    point = GeoPoint(float(top["lat"]), float(top["lon"]), str(top.get("display_name", city))[:300])
    db.cache_geocode(cache_key, point.lat, point.lon, point.label)
    return point


async def query_overpass(client: httpx.AsyncClient, settings: Settings, query: str) -> list[dict[str, Any]]:
    """Asks each server in turn: public Overpass instances are often busy, and the last failure is reported."""
    endpoints = (settings.overpass_url, *settings.overpass_mirrors)
    for position, url in enumerate(endpoints, start=1):
        try:
            return await _ask_overpass(client, url, settings, query)
        except OsmError as exc:
            log.warning("Overpass %s failed: %s", urlparse(url).netloc, exc)
            if position == len(endpoints):
                raise
            await asyncio.sleep(OVERPASS_RETRY_DELAY_S)
    return []  # unreachable: the last endpoint either returns or raises


async def _ask_overpass(client: httpx.AsyncClient, url: str, settings: Settings, query: str) -> list[dict[str, Any]]:
    try:
        response = await client.post(url, data={"data": query}, headers={"User-Agent": settings.osm_user_agent})
    except httpx.HTTPError as exc:
        raise OsmError("Overpass API недоступен, попробуйте позже.") from exc
    if response.status_code == 429:
        raise OsmError("Overpass API ограничил частоту запросов — подождите минуту и повторите.")
    if response.status_code >= 400:
        raise OsmError(f"Overpass API вернул ошибку {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OsmError("Overpass API вернул некорректный ответ.") from exc
    if not isinstance(payload, dict):
        raise OsmError("Overpass API вернул некорректный ответ.")
    # Timeouts/out-of-memory come back as HTTP 200 with a "remark" and empty or partial data.
    remark = str(payload.get("remark", ""))
    if "error" in remark.lower():
        raise OsmError("Overpass не успел обработать запрос — уменьшите радиус или число категорий.")
    return list(payload.get("elements", []))


def city_label_from(center: "GeoPoint", typed: str) -> str:
    """Canonical city name from Nominatim ("Казань, ..." → "Казань") so 'berlin'/'Berlin' match."""
    head = center.label.split(",")[0].strip()
    return (head or typed.strip())[:120]


async def search_businesses(
    settings: Settings,
    db: Database,
    city: str,
    radius_km: int,
    category_keys: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if radius_km not in ALLOWED_RADII_KM:
        raise OsmError("Недопустимый радиус.")
    categories = tuple(CATEGORY_BY_KEY[k] for k in category_keys if k in CATEGORY_BY_KEY)
    if not categories:
        raise OsmError("Выберите хотя бы одну категорию.")

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    try:
        center = await geocode(client, settings, db, city)
        elements = await query_overpass(
            client, settings, build_overpass_query(center.lat, center.lon, radius_km * 1000, categories)
        )
    finally:
        if owns_client:
            await client.aclose()

    city_label = city_label_from(center, city)
    radius_m = radius_km * 1000
    records = [
        r
        for r in (element_to_business(e, center, city_label, categories) for e in elements)
        if r is not None and r.distance_m <= radius_m
    ]
    # Up to MAX_RESULTS sqlite writes: one transaction, off the event loop.
    new = await asyncio.to_thread(db.upsert_businesses, records)

    return {
        "center": {"lat": center.lat, "lon": center.lon, "label": center.label},
        "city": city_label,
        "radius_km": radius_km,
        "found": len(records),
        "new": new,
        "opportunities": sum(r.opportunity for r in records),
        "truncated": len(elements) >= MAX_RESULTS,
    }
