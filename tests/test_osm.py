import json
import re

import httpx
import pytest

from leadhub import osm
from leadhub.osm import CATEGORY_BY_KEY, GeoPoint, OsmError

CENTER = GeoPoint(59.437, 24.745, "Tallinn")
CATS = (CATEGORY_BY_KEY["cafe"], CATEGORY_BY_KEY["barber"])


def _element(id_, tags, lat=59.438, lon=24.746, type_="node"):
    return {"type": type_, "id": id_, "lat": lat, "lon": lon, "tags": tags}


def _bbox(query):
    return [float(v) for v in re.search(r"\[bbox:([^\]]+)\]", query).group(1).split(",")]


def test_query_contains_only_whitelisted_filters_and_numbers():
    query = osm.build_overpass_query(59.4, 24.7, 3000, CATS)
    assert 'nwr["amenity"~"^(cafe)$"]["name"];' in query
    assert 'nwr["shop"~"^(hairdresser)$"]["name"];' in query
    assert query.startswith("[out:json]")
    assert "around" not in query  # a radius scan per clause is what timed out on large cities


def test_query_merges_categories_of_one_key_into_one_statement():
    query = osm.build_overpass_query(43.3454, -8.4138, 5000, (CATEGORY_BY_KEY["restaurant"], CATEGORY_BY_KEY["cafe"]))
    assert query.count("nwr[") == 1
    assert '"^(restaurant|cafe)$"' in query


def test_bounding_box_covers_the_radius_at_any_latitude():
    south, west, north, east = _bbox(osm.build_overpass_query(43.3454, -8.4138, 5000, CATS))
    assert south < 43.3454 < north and west < -8.4138 < east
    assert north - south == pytest.approx(0.0898, abs=0.001)
    assert east - west > north - south  # a degree of longitude is shorter away from the equator


def test_bounding_box_stays_valid_at_the_edges_of_the_map():
    _south, west, _north, east = _bbox(osm.build_overpass_query(-17.0, 179.99, 10_000, CATS))
    assert -180 <= west < east <= 180
    south, _west, north, _east = _bbox(osm.build_overpass_query(89.99, 10.0, 10_000, CATS))
    assert -90 <= south < north <= 90


def test_business_without_website_or_messenger_is_opportunity():
    record = osm.element_to_business(_element(1, {"name": "Kohvik", "amenity": "cafe"}), CENTER, "Tallinn", CATS)
    assert record.opportunity is True
    assert record.category == "cafe"
    assert 0 < record.distance_m < 500


@pytest.mark.parametrize("tags", [
    {"website": "https://cafe.ee"},
    {"contact:website": "cafe.ee"},
    {"contact:telegram": "@cafe"},
    {"contact:whatsapp": "+372555"},
])
def test_website_or_messenger_clears_flag(tags):
    record = osm.element_to_business(_element(2, {"name": "X", "amenity": "cafe", **tags}), CENTER, "T", CATS)
    assert record.opportunity is False


def test_social_profile_alone_keeps_flag_but_is_reported():
    record = osm.element_to_business(
        _element(3, {"name": "X", "shop": "hairdresser", "contact:instagram": "x"}), CENTER, "T", CATS
    )
    assert record.opportunity is True and record.social == "x"


def test_unnamed_or_uncategorised_elements_are_skipped():
    assert osm.element_to_business(_element(4, {"amenity": "cafe"}), CENTER, "T", CATS) is None
    assert osm.element_to_business(_element(5, {"name": "Bank", "amenity": "bank"}), CENTER, "T", CATS) is None


def test_way_uses_center_coordinates():
    element = {"type": "way", "id": 6, "center": {"lat": 59.44, "lon": 24.75}, "tags": {"name": "W", "amenity": "cafe"}}
    record = osm.element_to_business(element, CENTER, "T", CATS)
    assert record.osm_type == "way" and record.lat == 59.44


@pytest.mark.parametrize("raw, expected", [
    ("https://a.ee", "https://a.ee"),
    ("a.ee/menu", "https://a.ee/menu"),
    ("javascript:alert(1)", ""),
    ("https://a.ee; https://b.ee", "https://a.ee"),
])
def test_normalize_website_only_allows_http(raw, expected):
    assert osm.normalize_website(raw) == expected


def _mock_client(overpass_status=200, elements=None, geocode=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in request.url.host:
            return httpx.Response(200, json=geocode if geocode is not None else [
                {"lat": "59.437", "lon": "24.745", "display_name": "Tallinn, Estonia"}])
        return httpx.Response(overpass_status, json={"elements": elements or []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _search(settings, db, client, categories=("cafe",)):
    return await osm.search_businesses(settings, db, "Tallinn", 1, list(categories), client=client)


def test_search_stores_and_dedupes_by_osm_id(settings, db):
    import asyncio

    elements = [_element(10, {"name": "A", "amenity": "cafe"}), _element(11, {"name": "B", "amenity": "cafe",
                                                                                "website": "b.ee"})]
    first = asyncio.run(_search(settings, db, _mock_client(elements=elements)))
    assert (first["found"], first["new"], first["opportunities"]) == (2, 2, 1)

    again = asyncio.run(_search(settings, db, _mock_client(elements=elements)))
    assert again["new"] == 0
    assert db.list_businesses()[1] == 2
    assert db.cached_geocode("tallinn") is not None


def test_search_reports_unknown_city(settings, db):
    import asyncio

    with pytest.raises(OsmError, match="не найден"):
        asyncio.run(_search(settings, db, _mock_client(geocode=[])))


def test_search_reports_overpass_rate_limit(settings, db):
    import asyncio

    db.cache_geocode("tallinn", 59.4, 24.7, "Tallinn")
    with pytest.raises(OsmError, match="частоту"):
        asyncio.run(_search(settings, db, _mock_client(overpass_status=429)))


def test_overpass_gateway_error_falls_back_to_the_mirror(settings, db, monkeypatch):
    import asyncio

    monkeypatch.setattr(osm, "OVERPASS_RETRY_DELAY_S", 0)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return httpx.Response(504)
        return httpx.Response(200, json={"elements": [_element(20, {"name": "Retry Cafe", "amenity": "cafe"})]})

    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(_search(settings, db, client))["found"] == 1
    assert calls == ["overpass.test", "overpass-mirror.test"]


def test_overpass_timeout_falls_back_to_the_mirror(settings, db, monkeypatch):
    import asyncio

    monkeypatch.setattr(osm, "OVERPASS_RETRY_DELAY_S", 0)

    def handler(request):
        if request.url.host == "overpass.test":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"elements": [_element(21, {"name": "Slow Cafe", "amenity": "cafe"})]})

    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(_search(settings, db, client))["found"] == 1


def test_overpass_malformed_json_falls_back_to_the_mirror(settings, db, monkeypatch):
    import asyncio

    monkeypatch.setattr(osm, "OVERPASS_RETRY_DELAY_S", 0)

    def handler(request):
        if request.url.host == "overpass.test":
            return httpx.Response(200, json=["not", "an", "object"])
        return httpx.Response(200, json={"elements": [_element(22, {"name": "Good Cafe", "amenity": "cafe"})]})

    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(_search(settings, db, client))["found"] == 1


def test_overpass_persistent_gateway_error_is_reported(settings, db, monkeypatch):
    import asyncio

    monkeypatch.setattr(osm, "OVERPASS_RETRY_DELAY_S", 0)
    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn")
    with pytest.raises(OsmError, match="502"):
        asyncio.run(_search(settings, db, _mock_client(overpass_status=502)))


def test_businesses_outside_the_radius_are_not_stored(settings, db):
    import asyncio

    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn")
    elements = [_element(40, {"name": "Near", "amenity": "cafe"}), _element(41, {"name": "Corner", "amenity": "cafe"}, lat=59.446, lon=24.76)]
    result = asyncio.run(_search(settings, db, _mock_client(elements=elements)))  # radius 1 km; the corner is ~1.3 km away
    assert result["found"] == 1
    assert [b["name"] for b in db.list_businesses()[0]] == ["Near"]


@pytest.mark.parametrize("brand_tags", [{"brand": "Zara"}, {"brand:wikidata": "Q147662"}])
def test_chain_stores_are_not_opportunities(brand_tags):
    record = osm.element_to_business(_element(50, {"name": "Zara", "amenity": "cafe", **brand_tags}), CENTER, "T", CATS)
    assert record.opportunity is False


def test_geocoder_keeps_local_place_names(settings, db):
    import asyncio

    seen = []

    def handler(request):
        if "nominatim" in request.url.host:
            seen.append(request.url.params)
            return httpx.Response(200, json=[{"lat": "43.3454", "lon": "-8.4138", "display_name": "A Coruña, Galicia, España"}])
        return httpx.Response(200, json={"elements": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(osm.search_businesses(settings, db, "a coruna", 1, ["cafe"], client=client))
    assert result["city"] == "A Coruña"
    assert "accept-language" not in seen[0]


def test_overpass_runtime_remark_is_an_error(settings, db):
    import asyncio

    def handler(request):
        return httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out"})

    db.cache_geocode("tallinn", 59.4, 24.7, "Tallinn, Harju maakond, Eesti")
    with pytest.raises(OsmError, match="уменьшите радиус"):
        asyncio.run(_search(settings, db, httpx.AsyncClient(transport=httpx.MockTransport(handler))))


def test_city_label_is_canonical_from_geocoder(settings, db):
    import asyncio

    db.cache_geocode("tallinn", 59.437, 24.745, "Tallinn, Harju maakond, Eesti")
    result = asyncio.run(_search(settings, db, _mock_client(elements=[_element(30, {"name": "C", "amenity": "cafe"})])))
    assert result["city"] == "Tallinn"


def test_search_rejects_bad_radius(settings, db):
    import asyncio

    with pytest.raises(OsmError):
        asyncio.run(osm.search_businesses(settings, db, "Tallinn", 2, ["cafe"], client=_mock_client()))


def test_api_search_maps_osm_errors_to_502(authed, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise OsmError("Overpass API недоступен")

    monkeypatch.setattr(osm, "search_businesses", boom)
    response = authed.post("/api/osm/search", json={"city": "Tallinn", "radius_km": 1, "categories": ["cafe"]})
    assert response.status_code == 502
    assert json.loads(response.text)["detail"] == "Overpass API недоступен"
