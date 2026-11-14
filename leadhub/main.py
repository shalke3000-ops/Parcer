"""FastAPI app: page shell, password gate and JSON API."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth, osm
from .config import Settings, load_settings
from .csv_export import BUSINESS_COLUMNS, LEAD_COLUMNS, to_csv
from .db import MAX_NOTE_LENGTH, Database
from .lead_traits import MAX_BUDGET_EUR
from .scoring import MAX_SCORE

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
NONCE_PLACEHOLDER = "__CSP_NONCE__"
GZIP_MIN_BYTES = 1024
LONG_CACHE_PREFIXES = ("/static/fonts/", "/static/vendor/")

Status = Literal["new", "contacted", "replied", "client", "rejected"]
StatusFilter = Literal["", "new", "contacted", "replied", "client", "rejected"]
LeadSort = Literal["newest", "oldest", "budget"]


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class TrackingPatch(BaseModel):
    status: Status | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class OsmSearchBody(BaseModel):
    city: str = Field(min_length=2, max_length=120)
    radius_km: Literal[1, 3, 5, 10]
    categories: list[str] = Field(min_length=1, max_length=len(osm.CATEGORIES))

    @field_validator("city")
    @classmethod
    def strip_city(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("city too short")
        return value

    @field_validator("categories")
    @classmethod
    def known_categories(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in osm.CATEGORY_BY_KEY]
        if unknown:
            raise ValueError(f"unknown categories: {unknown}")
        return list(dict.fromkeys(values))


def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _csv_response(body: str, filename: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    throttle = auth.LoginThrottle()
    index_template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if settings.secret_key_is_ephemeral:
        log.warning("LEADHUB_SECRET_KEY not set: sessions will reset on every restart.")

    app = FastAPI(title="Lead Hub", docs_url=None, redoc_url=None, openapi_url=None)
    # Lead lists (long post texts) and vendored three.js compress ~4-5x; responses
    # never mix secrets with attacker-controlled input, so BREACH does not apply.
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith(LONG_CACHE_PREFIXES) and response.status_code == 200:
            # fonts and vendored libs have versioned/immutable file names
            response.headers["Cache-Control"] = "public, max-age=604800"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        nonce = secrets.token_urlsafe(18)
        html = index_template.replace(NONCE_PLACEHOLDER, nonce)
        return HTMLResponse(html, headers={"Content-Security-Policy": _csp(nonce), "Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- public auth endpoints --------------------------------------------
    public = APIRouter(prefix="/api")

    @public.get("/session")
    async def session(request: Request) -> dict:
        return {"authenticated": auth.is_authenticated(request, settings)}

    @public.post("/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict:
        if request.headers.get(auth.CSRF_HEADER) != "1":
            raise HTTPException(403, "Отсутствует заголовок X-LeadHub")
        key = _client_key(request)
        wait = throttle.retry_after(key)
        if wait:
            raise HTTPException(429, f"Слишком много попыток. Повторите через {wait} с.", headers={"Retry-After": str(wait)})
        if not auth.password_matches(settings, body.password):
            throttle.record_failure(key)
            raise HTTPException(401, "Неверный пароль")
        throttle.reset(key)
        auth.set_session_cookie(response, settings)
        return {"authenticated": True}

    @public.post("/logout")
    async def logout(response: Response) -> dict:
        auth.clear_session_cookie(response, settings)
        return {"authenticated": False}

    # ---- protected API: the dependency applies to every route below ----------
    api = APIRouter(prefix="/api", dependencies=[Depends(auth.make_auth_dependency(settings))])

    @api.get("/stats")
    def stats() -> dict:  # plain `def`: the trait counts scan the table, so this stays off the event loop
        return db.stats()

    @api.get("/sources")
    async def sources() -> dict:
        states = db.monitor_states()
        return {
            "telegram": {"configured": settings.telegram_configured, "chats": len(settings.tg_chats),
                         "state": states.get("telegram")},
            "reddit": {"configured": settings.reddit_configured, "subreddits": list(settings.reddit_subreddits),
                       "state": states.get("reddit")},
            "rss": {"configured": settings.rss_configured, "feeds": len(settings.rss_feeds),
                    "state": states.get("rss")},
            "notifications": {"configured": settings.notify_configured, "min_score": settings.notify_min_score},
            "min_score": settings.min_score,
            "max_score": MAX_SCORE,
        }

    def _lead_filters(
        status: StatusFilter = "",
        q: str = Query("", max_length=200),
        min_score: int = Query(0, ge=0, le=100),
        source: Literal["", "telegram", "reddit", "rss"] = "",
        sort: LeadSort = "newest",
        fixed_pay: bool = False,
        long_term: bool = False,
        one_off: bool = False,
        min_budget: float | None = Query(None, ge=0, le=MAX_BUDGET_EUR),  # euros
        max_budget: float | None = Query(None, ge=0, le=MAX_BUDGET_EUR),
    ) -> dict:
        return {
            "status": status or None, "query": q.strip() or None, "min_score": min_score, "source": source or None,
            "sort": sort, "fixed_pay": fixed_pay, "long_term": long_term, "one_off": one_off,
            "min_budget": min_budget, "max_budget": max_budget,
        }

    # Plain `def`: trait filters scan the table, so these run in the threadpool, not on the event loop.
    @api.get("/leads")
    def list_leads(
        filters: dict = Depends(_lead_filters),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        items, total = db.list_leads(**filters, limit=limit, offset=offset)
        return {"items": items, "total": total}

    @api.get("/leads/export.csv")
    def export_leads(filters: dict = Depends(_lead_filters)) -> Response:
        items, _ = db.list_leads(**filters, limit=None)
        return _csv_response(to_csv(items, LEAD_COLUMNS), "leads.csv")

    @api.patch("/leads/{lead_id}")
    async def patch_lead(lead_id: int, body: TrackingPatch) -> dict:
        updated = db.update_lead(lead_id, body.status, body.note)
        if updated is None:
            raise HTTPException(404, "Заказ не найден")
        return updated

    @api.get("/businesses")
    async def list_businesses(
        status: StatusFilter = "",
        q: str = Query("", max_length=200),
        opportunity_only: bool = False,
        city: str = Query("", max_length=120),
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        items, total = db.list_businesses(
            status=status or None, query=q.strip() or None, opportunity_only=opportunity_only,
            city=city or None, limit=limit, offset=offset,
        )
        return {"items": items, "total": total, "cities": db.business_cities()}

    @api.get("/businesses/export.csv")
    async def export_businesses(
        status: StatusFilter = "",
        q: str = Query("", max_length=200),
        opportunity_only: bool = False,
        city: str = Query("", max_length=120),
    ) -> Response:
        items, _ = db.list_businesses(
            status=status or None, query=q.strip() or None, opportunity_only=opportunity_only,
            city=city or None, limit=None,
        )
        return _csv_response(to_csv(items, BUSINESS_COLUMNS), "businesses.csv")

    @api.patch("/businesses/{biz_id}")
    async def patch_business(biz_id: int, body: TrackingPatch) -> dict:
        updated = db.update_business(biz_id, body.status, body.note)
        if updated is None:
            raise HTTPException(404, "Бизнес не найден")
        return updated

    @api.get("/osm/categories")
    async def osm_categories() -> dict:
        return {
            "categories": [{"key": c.key, "label": c.label, "default": c.default} for c in osm.CATEGORIES],
            "radii": list(osm.ALLOWED_RADII_KM),
        }

    @api.post("/osm/search")
    async def osm_search(body: OsmSearchBody) -> dict:
        try:
            return await osm.search_businesses(settings, db, body.city, body.radius_km, body.categories)
        except osm.OsmError as exc:
            raise HTTPException(502, str(exc)) from exc

    app.include_router(public)
    app.include_router(api)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    app.state.settings = settings
    app.state.db = db
    return app
