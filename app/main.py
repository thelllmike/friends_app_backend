"""FastAPI application entrypoint."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import admin, auth, me, meta, premium, safety, turn
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import setup_logging
from app.core.redis import redis
from app.tasks import scheduler
from app.ws import router as ws_router
from app.ws.manager import manager

setup_logging()
log = logging.getLogger("friends")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.check_production()
    log.info("starting %s env=%s", settings.app_name, settings.environment)
    await manager.start()
    tasks = scheduler.start()
    yield
    for t in tasks:
        t.cancel()
    await manager.stop()
    await redis.aclose()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
install_error_handlers(app)

for r in (meta.router, auth.router, me.router, turn.router, safety.router, premium.router, admin.router, ws_router.router):
    app.include_router(r)

# In production Caddy serves /media directly from the shared volume; this is a dev convenience.
if not settings.is_production:
    os.makedirs(settings.media_dir, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")


# ---- Admin dashboard (static SPA, same origin as the API so no CORS needed) -------------------------
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
DASHBOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                 "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
                 "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@app.middleware("http")
async def dashboard_headers(request: Request, call_next):
    path = request.url.path
    if path.startswith("/dashboard") and "/." in path:   # never serve .git, .env, other dotfiles
        return Response(status_code=404)
    response = await call_next(request)
    if path.startswith("/dashboard"):
        response.headers["Content-Security-Policy"] = DASHBOARD_CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-cache"
    return response


if os.path.isdir(DASHBOARD_DIR):
    @app.get("/dashboard", include_in_schema=False)
    async def _dashboard_root() -> RedirectResponse:
        return RedirectResponse("/dashboard/")

    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
