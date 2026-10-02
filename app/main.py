"""
Main entry point for the FastAPI application.
"""
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.sos import router as sos_router
from app.config import settings

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan events for startup and shutdown.
    """
    logger.info("Starting Suraksha SOS Module Backend...")
    # Ensure upload directory exists for media
    os.makedirs("uploads", exist_ok=True)
    
    from app.db.database import engine, create_tables
    from app.websocket.manager import ws_manager
    if settings.AUTO_CREATE_TABLES:
        await create_tables()
    try:
        yield
    finally:
        await ws_manager.close()
        await engine.dispose()

# Create FastAPI app instance
app = FastAPI(
    title="Suraksha SOS & Incident Reporting API",
    description="Backend services for Emergency Alerts, SOS, and Incident Reporting",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials="*" not in settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(sos_router)

@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint.
    """
    return {"status": "ok", "service": "suraksha-sos-module"}
