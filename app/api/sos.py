"""
FastAPI router for SOS endpoints.
"""
from typing import List
import time
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.schemas.sos import (
    SOSCreateRequest, SOSCancelRequest, SOSResponse, SOSEventResponse,
    MediaUploadResponse
)
from app.services import sos_service
from app.websocket.manager import ws_manager
from app.config import settings
from app.device_security import verify_origin
from app.auth import validate_token, require_subject, require_responder

router = APIRouter(prefix="/api/v1/sos", tags=["SOS"])

# Simple in-memory rate limiting dictionary: { user_id: [timestamps] }
_rate_limits = {}

async def check_rate_limit(user_id: str):
    """
    Checks if the user has exceeded the maximum allowed SOS events per minute.
    """
    if settings.REDIS_ENABLED:
        from app.realtime import redis_client
        try:
            count = await redis_client.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, f"sos:limit:{user_id}")
        except Exception:
            raise HTTPException(503, "Rate limiter unavailable; retry shortly") from None
        if count > settings.SOS_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, "Rate limit exceeded. Try again later.")
        return
    now = time.monotonic()
    for key in list(_rate_limits):
        if not _rate_limits[key] or now - _rate_limits[key][-1] >= 60:
            del _rate_limits[key]
    if user_id not in _rate_limits:
        _rate_limits[user_id] = []
        
    # Remove older entries beyond 1 minute
    _rate_limits[user_id] = [t for t in _rate_limits[user_id] if now - t < 60]
    
    if len(_rate_limits[user_id]) >= settings.SOS_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
        
    _rate_limits[user_id].append(now)

@router.post("", response_model=SOSResponse)
async def create_sos(
    sos_data: SOSCreateRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Submit a new SOS alert.
    """
    if sos_data.hop_count == 0:
        require_subject(claims, sos_data.user_id)
    else:
        if not sos_data.origin_proof:
            raise HTTPException(403, "Relayed alerts require a signed origin proof")
        verify_origin(sos_data.origin_proof, sos_data)
    if sos_data.hop_count == 0 and sos_data.origin_proof:
        verify_origin(sos_data.origin_proof, sos_data)
    await check_rate_limit(claims["sub"])

    return await sos_service.process_sos(db, sos_data, ws_manager)


@router.post("/cancel", response_model=SOSResponse)
async def cancel_sos(
    cancel_data: SOSCancelRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Cancel an active SOS alert.
    """
    require_subject(claims, cancel_data.user_id)
    return await sos_service.cancel_sos(db, cancel_data, ws_manager)


@router.get("/active", response_model=List[SOSEventResponse])
async def get_active_sos(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(50.0, gt=0, le=1000),
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Get active SOS alerts within a specified radius.
    """
    require_responder(claims)
    return await sos_service.get_active_sos_events(db, lat, lng, radius_km)


@router.post("/{sos_id}/acknowledge", response_model=SOSResponse)
async def acknowledge_sos(
    sos_id: str,
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Acknowledge an SOS alert (dashboard user or responder).
    """
    require_subject(claims, user_id)
    require_responder(claims)
    return await sos_service.acknowledge_sos(db, sos_id, user_id, ws_manager)


@router.post("/{sos_id}/respond", response_model=SOSResponse)
async def respond_to_sos(
    sos_id: str,
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Mark that a responder is en route to the SOS location.
    """
    require_subject(claims, user_id)
    # Any authenticated nearby volunteer can offer help; dashboard access remains restricted.
    return await sos_service.respond_to_sos(db, sos_id, user_id, ws_manager)


@router.post("/media", response_model=MediaUploadResponse)
async def upload_media(
    sos_id: str = Form(...),
    uploaded_by: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Upload media attached to an SOS alert.
    """
    require_subject(claims, uploaded_by)
    try:
        return await sos_service.save_media(db, sos_id, file, uploaded_by)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.websocket("/ws/sos")
async def websocket_endpoint(websocket: WebSocket, client_id: str = Query(...)):
    """
    WebSocket endpoint for real-time SOS broadcasts.
    """
    try:
        claims = validate_token(websocket.headers.get("authorization"))
        require_responder(claims)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await ws_manager.connect(websocket, client_id)
    try:
        while True:
            # Keep connection alive and listen for client messages if needed
            remaining = max(0, float(claims["exp"]) - time.time())
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                await websocket.close(code=1008)
                return
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(client_id, websocket)


@router.get("/media/{attachment_id}")
async def read_media(attachment_id: str, db: AsyncSession = Depends(get_db),
                     claims: dict = Depends(validate_token)):
    from pathlib import Path
    from fastapi.responses import FileResponse
    from app.models.sos import MediaAttachment
    attachment = await db.get(MediaAttachment, attachment_id)
    if not attachment:
        raise HTTPException(404, "Attachment not found")
    if claims["sub"] != attachment.uploaded_by:
        require_responder(claims)
    # IDs come from the database; never interpret client input as a filesystem path.
    paths = list(Path("uploads").glob(f"{attachment.id}.*"))
    if not paths:
        raise HTTPException(404, "Media file not found")
    return FileResponse(paths[0])
