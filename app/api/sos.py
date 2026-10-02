"""
FastAPI router for SOS endpoints.
"""
from typing import List, Optional
import time
from fastapi import APIRouter, Depends, HTTPException, Header, Query, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.schemas.sos import (
    SOSCreateRequest, SOSCancelRequest, SOSResponse, SOSEventResponse,
    MediaUploadResponse
)
from app.services import sos_service
from app.websocket.manager import ws_manager
from app.config import settings

router = APIRouter(prefix="/api/v1/sos", tags=["SOS"])

# Simple in-memory rate limiting dictionary: { user_id: [timestamps] }
_rate_limits = {}

def check_rate_limit(user_id: str):
    """
    Checks if the user has exceeded the maximum allowed SOS events per minute.
    """
    now = time.time()
    if user_id not in _rate_limits:
        _rate_limits[user_id] = []
        
    # Remove older entries beyond 1 minute
    _rate_limits[user_id] = [t for t in _rate_limits[user_id] if now - t < 60]
    
    if len(_rate_limits[user_id]) >= settings.SOS_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
        
    _rate_limits[user_id].append(now)

def validate_token(authorization: Optional[str] = Header(None)):
    """
    Validates JWT token for direct online SOS requests.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
    # In a real app we'd decode and verify the JWT here.
    # For now, this is a placeholder check.
    return True

@router.post("", response_model=SOSResponse)
async def create_sos(
    sos_data: SOSCreateRequest,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    """
    Submit a new SOS alert.
    """
    # Validate token only if it's a direct online request (hop_count == 0)
    if sos_data.hop_count == 0:
        validate_token(authorization)
        
    check_rate_limit(sos_data.user_id)
    
    return await sos_service.process_sos(db, sos_data, ws_manager)


@router.post("/cancel", response_model=SOSResponse)
async def cancel_sos(
    cancel_data: SOSCancelRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Cancel an active SOS alert.
    """
    return await sos_service.cancel_sos(db, cancel_data, ws_manager)


@router.get("/active", response_model=List[SOSEventResponse])
async def get_active_sos(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(50.0, gt=0),
    db: AsyncSession = Depends(get_db)
):
    """
    Get active SOS alerts within a specified radius.
    """
    return await sos_service.get_active_sos_events(db, lat, lng, radius_km)


@router.post("/{sos_id}/acknowledge", response_model=SOSResponse)
async def acknowledge_sos(
    sos_id: str,
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Acknowledge an SOS alert (dashboard user or responder).
    """
    return await sos_service.acknowledge_sos(db, sos_id, user_id, ws_manager)


@router.post("/{sos_id}/respond", response_model=SOSResponse)
async def respond_to_sos(
    sos_id: str,
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Mark that a responder is en route to the SOS location.
    """
    return await sos_service.respond_to_sos(db, sos_id, user_id, ws_manager)


@router.post("/media", response_model=MediaUploadResponse)
async def upload_media(
    sos_id: str = Form(...),
    uploaded_by: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Upload media attached to an SOS alert.
    """
    try:
        return await sos_service.save_media(db, sos_id, file, uploaded_by)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.websocket("/ws/sos")
async def websocket_endpoint(websocket: WebSocket, client_id: str = Query(...)):
    """
    WebSocket endpoint for real-time SOS broadcasts.
    """
    await ws_manager.connect(websocket, client_id)
    try:
        while True:
            # Keep connection alive and listen for client messages if needed
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(client_id)
