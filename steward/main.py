"""FastAPI application factory. Run with:  uvicorn steward.asgi:app"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from steward import __version__
from steward.agent.llm import make_provider
from steward.api import agent_routes, auth_routes, finance_routes
from steward.config import ROOT_DIR, Settings, get_settings
from steward.db import init_db, make_engine, make_session_factory
from steward.security.ratelimit import RateLimiter

log = logging.getLogger("steward")
WEB_DIR = ROOT_DIR / "web"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)


def create_app(settings: Settings | None = None, llm_provider=None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)

    app = FastAPI(
        title="Steward API",
        version=__version__,
        description="AI personal-finance participant: budgeting, bill analysis and spending insights, acting on a "
                    "sandbox bank inside user-chosen permission tiers.",
        docs_url=None if settings.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.limiter = RateLimiter()
    app.state.llm = llm_provider or make_provider()

    @app.middleware("http")
    async def security_and_limits(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        ip = request.client.host if request.client else "unknown"
        if request.url.path.startswith("/api/") and not app.state.limiter.hit(f"global:{ip}", 600, 60):
            return JSONResponse({"error": {"code": "rate_limited", "message": "Too many requests."}}, status_code=429)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = CSP
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        # Log method, path, status and timing only: never bodies, cookies or query strings.
        log.info("%s %s %s %.1fms rid=%s", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - start) * 1000, request_id)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return JSONResponse({"error": {"code": "invalid_request", "message": "; ".join(msgs)}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"error": {"code": "internal_error", "message": "Something went wrong."}}, status_code=500)

    for router in (auth_routes.router, finance_routes.router, agent_routes.router):
        app.include_router(router, prefix="/api/v1")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/meta")
    def meta() -> dict:
        return {"version": __version__, "llm_provider": app.state.llm.name,
                "llm_model": getattr(app.state.llm, "model", None), "currency": settings.currency}

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(Path(WEB_DIR, "index.html"))

    return app
