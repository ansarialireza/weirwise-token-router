from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.api.admin import admin_router
from token_router.api.router import router as route_router
from token_router.core.config import settings
from token_router.db.session import Base, engine, get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup tables if needed
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Production-grade, independent Provider and Credential Routing Service for Weirwise & external SaaS ecosystems.",
    docs_url="/docs" if settings.DEBUG or settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)

if settings.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/health/live", tags=["Diagnostics"])
async def health_live():
    return {"status": "alive", "version": settings.VERSION}


@app.get("/health/ready", tags=["Diagnostics"])
async def health_ready(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"unhealthy: {type(e).__name__}"

    return {
        "status": "ready" if db_status == "connected" else "degraded",
        "database": db_status,
        "version": settings.VERSION,
    }


app.include_router(route_router)
app.include_router(admin_router)
