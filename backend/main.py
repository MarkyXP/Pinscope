"""PinscopeX backend — FastAPI application."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pip_system_certs.wrapt_requests
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

# ------------------------------------------------------------------
#  SSL - Patch certifi package to the the local machine cert store
# ------------------------------------------------------------------
pip_system_certs.wrapt_requests.inject_truststore()

from backend.config import settings
from backend.routers import (
    admin,
    contact,
    feedback,
    pipeline,
    projects,
    reports,
    survey,
)
from backend.services.projects import ProjectNotFound
from backend.services.storage import LocalStorageBackend

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / ".env")

# Default user ID for unauthenticated local dev
LOCAL_DEV_USER = "local"


def _create_storage():
    """Create the appropriate storage backend based on config."""
    if settings.use_gcs:
        from backend.services.storage_gcs import GCSStorageBackend

        return GCSStorageBackend(settings.gcs_bucket)
    return LocalStorageBackend(settings.data_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Guard: refuse to start in production without authentication
    env = os.getenv("ENVIRONMENT", "").lower()
    if env == "production" and not settings.use_auth:
        raise RuntimeError(
            "CLERK_JWKS_URL and CLERK_SECRET_KEY must be set in production. "
            "Authentication cannot be disabled in production."
        )
    if not settings.use_auth:
        logger.warning(
            "Authentication is DISABLED — all users have full access. "
            "This is only safe for local development."
        )
    if not settings.billing_enabled:
        logger.warning(
            "Billing is DISABLED — pipelines run free and the billing/credits "
            "routes are not mounted."
        )

    app.state.storage = _create_storage()

    # For local backend, ensure base directories exist
    if isinstance(app.state.storage, LocalStorageBackend):
        base = settings.data_dir
        (base / "users").mkdir(parents=True, exist_ok=True)
        (base / "library" / "extracted").mkdir(parents=True, exist_ok=True)
        (base / "library" / "patterns").mkdir(parents=True, exist_ok=True)
        (base / "library" / "models").mkdir(parents=True, exist_ok=True)
    yield
    # Pipelines run in a separate Cloud Run Job worker (or local
    # subprocess in dev), so the API process has nothing to clean up
    # on shutdown.


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.use_auth:
            # Only set HSTS when running behind TLS in production
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract user_id from Clerk JWT or default to local dev user."""

    async def dispatch(self, request: Request, call_next):
        # Let CORS preflight through — browsers send OPTIONS without credentials
        if request.method == "OPTIONS":
            return await call_next(request)
        # Public endpoints that don't require authentication
        if request.url.path == "/api/contact":
            request.state.user_id = LOCAL_DEV_USER
            return await call_next(request)
        if settings.use_auth:
            from backend.middleware.auth import verify_clerk_token

            user_id = await verify_clerk_token(request)
            if user_id is None:
                is_production = os.getenv("ENVIRONMENT", "").lower() == "production"
                if is_production:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required"},
                    )
                # Non-production: fall back to local dev user so Clerk config
                # doesn't block local development when no token is present.
                user_id = LOCAL_DEV_USER
            request.state.user_id = user_id
        else:
            request.state.user_id = LOCAL_DEV_USER
        response = await call_next(request)
        return response


app = FastAPI(
    title="PinscopeX",
    description="Agentic schematic validation API",
    lifespan=lifespan,
)

# Middleware order matters: Starlette applies in LIFO order (last added =
# outermost).  CORSMiddleware MUST be outermost so that CORS headers are
# present on every response — including 401s from AuthMiddleware.
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["content-type", "authorization"],
    expose_headers=["X-Datasheet-Url"],
)


@app.exception_handler(ProjectNotFound)
async def _project_not_found_handler(request: Request, exc: ProjectNotFound):
    # A mutation raced a project deletion (or hit never-fully-created metadata).
    # Return a clean 404 — CORSMiddleware is outermost, so headers still land.
    return JSONResponse(status_code=404, content={"detail": str(exc)})


app.include_router(projects.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(reports.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
if settings.billing_enabled:
    # Import guarded too: with billing disabled the core never loads the
    # billing/credits routers (or, transitively, the Stripe SDK).
    from backend.routers import billing, credits

    app.include_router(billing.router, prefix="/api")
    app.include_router(credits.router, prefix="/api")
app.include_router(contact.router, prefix="/api")
app.include_router(feedback.router, prefix="/api")
app.include_router(survey.router, prefix="/api")

# Debug-only endpoints — only when APP_VERSION is not set (i.e. debug mode)
if settings.is_debug:
    from backend.routers import debug

    app.include_router(debug.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
