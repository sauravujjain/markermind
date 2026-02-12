from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from .config import settings
from .database import engine, Base
from .api.routes import (
    auth_router,
    orders_router,
    patterns_router,
    fabrics_router,
    nesting_router,
    cutplans_router,
    exports_router,
    costs_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    # Note: In production, use Alembic migrations instead
    # Base.metadata.create_all(bind=engine)
    yield
    # Shutdown
    pass


app = FastAPI(
    title="MarkerMind API",
    description="Cutting Optimization Platform for Garment Manufacturing",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(patterns_router, prefix="/api")
app.include_router(fabrics_router, prefix="/api")
app.include_router(nesting_router, prefix="/api")
app.include_router(cutplans_router, prefix="/api")
app.include_router(exports_router, prefix="/api")
app.include_router(costs_router, prefix="/api")


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "MarkerMind API", "version": "1.0.0"}


@app.get("/health")
async def health():
    """Detailed health check."""
    return {
        "status": "healthy",
        "database": "connected",  # TODO: Add actual DB check
        "redis": "connected",  # TODO: Add actual Redis check
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )
