import re

from fastapi.routing import APIRoute

from leadhub.db import BusinessRecord, NewLead

PUBLIC_API = {"/api/session", "/api/login", "/api/logout"}


def _lead(external_id="t3_1", score=7, text="[Hiring] need a telegram bot", source="reddit"):
    return NewLead(source, "r/forhire", external_id, text, "https://reddit.com/x", "u/a", "2026-11-07T10:00:00+00:00",
                   score, ("hiring", "telegram bot"))


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _all_routes(app):
    """Flatten routes; newer FastAPI wraps included routers lazily."""
    for route in app.routes:
        nested = getattr(route, "original_router", None)
        yield from (nested.routes if nested is not None else [route])


def test_every_protected_api_route_rejects_anonymous_requests(app, client):
    protected = [r for r in _all_routes(app)
                 if isinstance(r, APIRoute) and r.path.startswith("/api") and r.path not in PUBLIC_API]
    assert len(protected) >= 10  # guard against the check silently testing nothing

    for route in protected:
        for method in route.methods:
            response = client.request(method, _concrete(route.path), headers={"X-LeadHub": "1"}, json={})
            assert response.status_code == 401, f"{method} {route.path} returned {response.status_code}"


def test_changing_password_invalidates_existing_sessions(settings):
    from dataclasses import replace

    from leadhub import auth

    token = auth.issue_token(settings)
    assert auth.verify_token(settings, token)
    assert not auth.verify_token(replace(settings, password="a different password"), token)


def test_expired_token_is_rejected(settings):
    from leadhub import auth

    token = auth.issue_token(settings, now=1_000_000)
    assert not auth.verify_token(settings, token)


def test_hint_cookie_alone_grants_nothing(client):
    client.cookies.set("leadhub_hint", "1")
    assert client.get("/api/stats").status_code == 401


def test_forged_cookie_is_rejected(client):
    client.cookies.set("leadhub_session", "9999999999.abc.forged")
    assert client.get("/api/stats").status_code == 401


def test_login_wrong_password_then_throttled(client):
    headers = {"X-LeadHub": "1"}
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}, headers=headers).status_code == 401
    assert client.post("/api/login", json={"password": "nope"}, headers=headers).status_code == 429


def test_login_requires_csrf_header(client):
    from tests.conftest import PASSWORD

    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_mutation_without_csrf_header_is_forbidden(authed, db):
    lead_id = db.insert_lead(_lead())
    authed.headers.pop("X-LeadHub")
    assert authed.patch(f"/api/leads/{lead_id}", json={"status": "contacted"}).status_code == 403


def test_session_cookie_flags(client):
    from tests.conftest import PASSWORD

    response = client.post("/api/login", json={"password": PASSWORD}, headers={"X-LeadHub": "1"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_large_responses_are_gzipped(authed, db):
    for i in range(20):
        db.insert_lead(_lead(f"g{i}", text="need a python telegram bot " * 40))
    response = authed.get("/api/leads", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"


def test_index_has_csp_with_nonce(client):
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in response.text
    assert "__CSP_NONCE__" not in response.text


def test_list_filter_and_update_lead(authed, db):
    first = db.insert_lead(_lead("a", score=9, text="[Hiring] python scraper"))
    db.insert_lead(_lead("b", score=5, text="need landing page"))

    assert authed.get("/api/leads", params={"min_score": 8}).json()["total"] == 1
    assert authed.get("/api/leads", params={"q": "landing"}).json()["total"] == 1

    updated = authed.patch(f"/api/leads/{first}", json={"status": "contacted", "note": "wrote on reddit"}).json()
    assert updated["status"] == "contacted" and updated["note"] == "wrote on reddit"
    assert authed.get("/api/leads", params={"status": "contacted"}).json()["total"] == 1


def test_invalid_status_is_rejected(authed, db):
    lead_id = db.insert_lead(_lead())
    assert authed.patch(f"/api/leads/{lead_id}", json={"status": "won"}).status_code == 422


def test_patch_missing_lead_is_404(authed):
    assert authed.patch("/api/leads/999", json={"status": "client"}).status_code == 404


def test_search_with_like_wildcards_is_literal(authed, db):
    db.insert_lead(_lead("a", text="need bot 100% done"))
    db.insert_lead(_lead("b", text="need bot fast"))
    assert authed.get("/api/leads", params={"q": "100%"}).json()["total"] == 1


def test_export_leads_csv_escapes_formulas(authed, db):
    db.insert_lead(_lead("x", text="=HYPERLINK(\"http://evil\")"))
    body = authed.get("/api/leads/export.csv").content.decode("utf-8-sig")
    assert "\"'=HYPERLINK" in body


def test_business_listing_update_and_export(authed, db):
    db.upsert_business(BusinessRecord("node", 1, "Кофе Тут", "cafe", 59.0, 24.0, "Main 1", "", "", "", "", True,
                                      "Tallinn", 120, {}))
    data = authed.get("/api/businesses", params={"opportunity_only": True}).json()
    assert data["total"] == 1 and data["cities"] == ["Tallinn"]

    biz_id = data["items"][0]["id"]
    assert authed.patch(f"/api/businesses/{biz_id}", json={"note": "call Monday"}).json()["note"] == "call Monday"
    assert "Кофе Тут" in authed.get("/api/businesses/export.csv").content.decode("utf-8-sig")


def test_stats_and_sources_never_leak_secrets(authed, settings):
    stats = authed.get("/api/stats").json()
    assert stats["leads"]["total"] == 0
    sources = authed.get("/api/sources").text
    for secret in (settings.secret_key, settings.password):
        assert secret not in sources


def test_osm_search_validates_input(authed):
    assert authed.post("/api/osm/search", json={"city": "X", "radius_km": 2, "categories": ["cafe"]}).status_code == 422
    assert authed.post("/api/osm/search", json={"city": "Riga", "radius_km": 1, "categories": ["casino"]}).status_code == 422


def test_logout_clears_session(authed):
    authed.post("/api/logout")
    assert authed.get("/api/stats").status_code == 401


def _timed(external_id, posted_at, text):
    return NewLead("rss", "Feed", external_id, text, "", "", posted_at, 7, ())


def test_leads_api_sorts_and_filters_by_traits(authed, db):
    db.insert_lead(_timed("old", "2026-01-01T00:00:00+00:00", "Fixed price, full-time"))
    db.insert_lead(_timed("new", "2026-02-01T00:00:00+00:00", "Fixed price only"))

    ids = lambda query: [i["external_id"] for i in authed.get(f"/api/leads?{query}").json()["items"]]
    assert ids("sort=oldest") == ["old", "new"]
    assert ids("sort=newest") == ["new", "old"]
    assert ids("long_term=true") == ["old"]
    assert ids("fixed_pay=true&long_term=true") == ["old"]


def test_leads_api_filters_one_off_work(authed, db):
    db.insert_lead(_timed("gig", "2026-01-01T00:00:00+00:00", "Разовая задача, нужен бот"))
    db.insert_lead(_timed("job", "2026-01-02T00:00:00+00:00", "Full-time engineer needed"))

    items = authed.get("/api/leads?one_off=true").json()["items"]
    assert [i["external_id"] for i in items] == ["gig"]


def test_leads_api_rejects_unknown_sort(authed):
    assert authed.get("/api/leads?sort=random").status_code == 422


def test_leads_api_filters_and_sorts_by_budget(authed, db):
    db.insert_lead(_timed("small", "2026-01-01T00:00:00+00:00", "Need a bot, $100"))
    db.insert_lead(_timed("big", "2026-01-02T00:00:00+00:00", "Need a shop, €900"))
    db.insert_lead(_timed("none", "2026-01-03T00:00:00+00:00", "Need a site, details later"))

    items = authed.get("/api/leads?min_budget=500").json()["items"]
    assert [i["external_id"] for i in items] == ["big"] and items[0]["budget_eur"] == 900
    by_price = authed.get("/api/leads?sort=budget").json()["items"]
    assert [i["external_id"] for i in by_price] == ["big", "small", "none"]
    assert authed.get("/api/leads?min_budget=-5").status_code == 422
    assert "budget_eur" in authed.get("/api/leads/export.csv").text.splitlines()[0]


def test_lead_export_honours_trait_filters(authed, db):
    db.insert_lead(_timed("fx", "2026-01-01T00:00:00+00:00", "Fixed price, need a bot"))
    db.insert_lead(_timed("no", "2026-01-02T00:00:00+00:00", "Need a bot, details later"))

    body = authed.get("/api/leads/export.csv?fixed_pay=true").text
    assert "Fixed price" in body and "details later" not in body


def test_static_3d_fallback_styles_can_reach_inside_use(client):
    # Selectors through the <svg> host never match inside <use>; the ellipses then render solid black.
    page = client.get("/").text
    assert "#lux-static .ring" in page
    assert ".lux-art__static .ring" not in page


def test_static_starfield_backdrop_and_motion_toggle(client):
    page = client.get("/").text
    assert 'id="starfield"' in page and "scheduleStarfield();" in page
    assert "count: 4200" in page and "drift: 2.35" in page and 'colorA: "#aef6cf"' in page
    assert "GALAXY" not in page  # the old backdrop is gone, not drawn underneath
    # The viewer's own choice beats the OS setting, so the button must exist and CSS must follow it.
    assert 'id="motion-toggle"' in page and ':root[data-motion="reduce"]' in page
    assert "prefers-reduced-motion: reduce) {" not in page


def test_motion_switch_overrides_os_reduced_motion(client):
    # CSS must follow the in-app switch: a raw media query would freeze motion the user turned back on.
    page = client.get("/").text
    assert 'id="motion-toggle"' in page and 'id="starfield"' in page
    assert ":root[data-motion=\"reduce\"]" in page
    assert "@media (prefers-reduced-motion" not in page and "prefers-reduced-motion: no-preference" not in page


def test_page_polls_for_new_leads_while_open(client):
    # The monitors add leads while the window is open; the page must pick them up without a restart.
    page = client.get("/").text
    assert 'id="lead-new"' in page and "LIVE_POLL_MS" in page and "startLivePolling();" in page
