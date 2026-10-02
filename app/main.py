"""
Main entry point for the FastAPI application.
"""
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
    
    # Initialize Database Tables automatically (helpful for Docker PostGIS)
    from app.db.database import engine, Base
    import app.models.sos  # Import models so Base metadata is aware
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    yield
    logger.info("Shutting down Suraksha SOS Module Backend...")
    # Any cleanup tasks can go here

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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(sos_router)

# Mount static files for media uploads
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="uploads"), name="static")

@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint.
    """
    return {"status": "ok", "service": "suraksha-sos-module"}
