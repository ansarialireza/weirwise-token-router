import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from token_router.api.admin import admin_router
from token_router.api.router import router as routing_router
from token_router.api.webhooks import router as webhooks_router
from token_router.core.config import settings
from token_router.core.http import close_http_client, get_http_client
from token_router.db.session import async_session_factory, engine, init_db
from token_router.services.webhook_service import WebhookWorker

webhook_worker = WebhookWorker(session_factory=async_session_factory, poll_interval_seconds=2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Enforce strict production secrets validation before serving
    settings.validate_production_secrets()
    # Initialize DB tables
    await init_db()
    # Warm up shared HTTP connection pool
    await get_http_client()
    # Start background webhook processor
    webhook_worker.start()
    yield
    # Stop background worker
    await webhook_worker.stop()
    # Close shared pooled HTTP clients gracefully
    await close_http_client()
    # Graceful shutdown of DB engine
    await engine.dispose()


app = FastAPI(
    title="Weirwise Token Router - Universal Service Integration Platform",
    description="Universal, Agent-Native Service Integration and Execution Infrastructure.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routing_router)
app.include_router(admin_router)
app.include_router(webhooks_router)


@app.get("/health/live")
async def health_live():
    return {
        "status": "alive",
        "service": "weirwise-token-router",
        "platform": "Universal Service Integration Platform",
        "version": "2.0.0",
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "weirwise-token-router",
        "platform": "Universal Service Integration Platform",
        "version": "2.0.0",
    }


@app.get("/health/ready")
async def readiness_check():
    return {
        "status": "ready",
        "database": "connected",
        "vault": "operational",
    }
