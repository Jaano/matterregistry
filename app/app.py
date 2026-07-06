import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .settings import Settings
from .web.routes import router as web_router

_STATIC_DIR = Path(__file__).parent / "web" / "static"
logger = logging.getLogger(__name__)


async def _resolve_matter_url(settings: Settings) -> str | None:
    """Return the Matter Server WS URL: env → HA option → None."""
    env_url = os.environ.get("PYTHON_MATTER_SERVER", "").strip()
    if env_url:
        return env_url

    if settings.option_python_matter_server_url:
        return settings.option_python_matter_server_url

    return None


async def _maybe_start_matter_client(settings: Settings):
    """Start the MatterServerClient if a URL is configured; return client or None."""
    from .integrations.matter_server.server_client import MatterServerClient

    url = await _resolve_matter_url(settings)
    if not url:
        logger.info("Matter Server integration disabled (no URL configured)")
        return None

    logger.info("Starting Matter Server client → %s", url)
    client = MatterServerClient(url)
    await client.start()
    return client


async def _resolve_otbr_url(settings: Settings) -> str | None:
    """Return the OTBR base URL: env → HA option → None."""
    env_url = os.environ.get("OTBR_URL", "").strip()
    if env_url:
        return env_url

    if settings.option_otbr_url:
        return settings.option_otbr_url

    return None


async def _maybe_start_otbr_client(settings: Settings):
    """Start the OTBRClient if a URL is configured; return client or None."""
    from .integrations.otbr.client import OTBRClient

    url = await _resolve_otbr_url(settings)
    if not url:
        logger.info("OTBR integration disabled (no URL configured)")
        return None

    logger.info("Starting OTBR client → %s", url)
    client = OTBRClient(url)
    await client.start()
    return client


async def _resolve_ha_core(settings: Settings) -> tuple[str | None, str | None]:
    """Return (url, token) for HA Core, or (None, None) if not configured.

    HA App: Supervisor token takes priority over everything else.
    Standalone: env HA_CORE_URL / HA_CORE_TOKEN → HA option.
    """
    import os as _os

    supervisor_token = _os.environ.get("SUPERVISOR_TOKEN", "")

    if supervisor_token:
        return "http://supervisor/core", supervisor_token

    url = _os.environ.get("HA_CORE_URL", "").strip() or settings.option_ha_core_url
    token = _os.environ.get("HA_CORE_TOKEN", "").strip() or settings.option_ha_core_token

    if url and token:
        return url, token
    return None, None


async def _maybe_start_ha_client(settings: Settings):
    """Start the HACoreClient if configured; return client or None."""
    from .integrations.ha.client import HACoreClient

    url, token = await _resolve_ha_core(settings)
    if not url or not token:
        logger.info("HA Core integration disabled (no URL/token configured)")
        return None

    logger.info("Starting HA Core client → %s", url)
    client = HACoreClient(url, token)
    await client.start()
    return client


async def _maybe_start_mdns_client(settings: Settings):
    """Start the mDNS HomeKit discovery client if enabled; return client or None.

    Opt-in (needs host networking): MDNS_ENABLED / the mdns_enabled HA option.
    """
    if not settings.mdns_enabled:
        logger.info("mDNS discovery disabled (MDNS_ENABLED not set)")
        return None

    from .integrations.mdns.client import MdnsClient

    logger.info("Starting mDNS HomeKit discovery client")
    client = MdnsClient()
    await client.start()
    return client


async def _kick_sync_all(app) -> None:
    """Run one pass of all active integration syncs. Each failure is logged and swallowed."""
    integrations = getattr(app.state, "integrations", [])
    for integration in integrations:
        try:
            await integration.sync_now()
        except Exception:
            logger.exception("Auto-sync: %s failed", integration.slug)


async def _sync_loop(app, interval: int) -> None:
    """Background task: wait interval seconds, then kick all syncs, repeat."""
    try:
        while True:
            await asyncio.sleep(interval)
            logger.info("Auto-sync tick (interval=%ds)", interval)
            await _kick_sync_all(app)
    except asyncio.CancelledError:
        pass


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(f"Migration failed:\n{result.stderr}")

        matter_client = await _maybe_start_matter_client(settings)
        otbr_client = await _maybe_start_otbr_client(settings)
        ha_client = await _maybe_start_ha_client(settings)
        mdns_client = await _maybe_start_mdns_client(settings)
        app.state.matter_client = matter_client
        app.state.otbr_client = otbr_client
        app.state.ha_client = ha_client
        app.state.mdns_client = mdns_client

        # Populate registry: one entry per configured+started integration.
        # Ordered: Matter (event-driven) first, then HA Core (polls), then OTBR,
        # then mDNS discovery.
        app.state.integrations = [
            c for c in [matter_client, ha_client, otbr_client, mdns_client] if c is not None
        ]

        interval = settings.integration_sync_interval
        sync_task: asyncio.Task | None = None
        if interval != -1:
            asyncio.create_task(_kick_sync_all(app))  # startup sync (fire-and-forget)
        if interval > 0:
            sync_task = asyncio.create_task(_sync_loop(app, interval))
        app.state.sync_task = sync_task

        try:
            yield
        finally:
            if sync_task is not None:
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            if matter_client:
                await matter_client.stop()
            if otbr_client:
                await otbr_client.stop()
            if ha_client:
                await ha_client.stop()
            if mdns_client:
                await mdns_client.stop()

    app = FastAPI(
        title="Matter Registry",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def ingress_middleware(request, call_next):
        # Store ingress path for URL generation in templates/responses.
        request.state.ingress_path = request.headers.get("X-Ingress-Path", "")

        # HA App mode with host_network: block direct port access unless the
        # request carries X-Ingress-Path (set by Supervisor on authenticated
        # Ingress sessions) or direct_api is explicitly enabled.
        # Supervisor validates the user session before forwarding and only sets
        # this header on authenticated requests, so its presence is sufficient.
        # /healthz is always allowed (Supervisor health probe).
        if (
            settings.ha_mode
            and not settings.option_direct_api
            and not request.state.ingress_path
            and request.url.path != "/healthz"
        ):
            return JSONResponse({"detail": "Access via Ingress only"}, status_code=403)

        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok", "version": settings.version})

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        ingress_path = getattr(request.state, "ingress_path", "").rstrip("/")
        return RedirectResponse(f"{ingress_path}/devices")

    app.include_router(api_router, prefix="/api")
    app.include_router(web_router)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app
