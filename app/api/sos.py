"""
FastAPI router for SOS endpoints.
"""
from typing import List
import asyncio
import time
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

# Compatibility export for existing test fixtures.
from app.services.rate_limit import _windows as _rate_limits
from app.services.receipts import server_receipt

@router.post("", response_model=SOSResponse)
async def create_sos(
    sos_data: SOSCreateRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token)
):
    """
    Submit a new SOS alert.
    """
    if sos_data.message_type.value != 'SOS_ALERT':
        raise HTTPException(422, 'Use /sos/mesh-cancel for signed cancellation')
    if (sos_data.revision or sos_data.protocol_version == 3) and not sos_data.origin_proof:
        raise HTTPException(403, 'Version 3 alerts and location updates require a signed origin proof')
    relayed = sos_data.hop_count > 0 or sos_data.delivery_method.value == "BLE_RELAY"
    if not relayed:
        require_subject(claims, sos_data.user_id)
    else:
        if not sos_data.origin_proof:
            raise HTTPException(403, "Relayed alerts require a signed origin proof")
        verify_origin(sos_data.origin_proof, sos_data)
    if not relayed and sos_data.origin_proof:
        verify_origin(sos_data.origin_proof, sos_data)
    try:
        response = await sos_service.process_sos(db, sos_data, ws_manager, actor_id=claims["sub"])
    except HTTPException as error:
        if error.status_code == 410 and error.detail == "SOS has been cancelled":
            raise HTTPException(410, {"message": error.detail, "receipt": server_receipt(sos_data, 'cancelled')}) from None
        raise
    response.receipt = server_receipt(sos_data, 'accepted')
    return response


@router.post('/mesh-cancel', response_model=SOSResponse)
async def mesh_cancel(sos_data: SOSCreateRequest, db: AsyncSession = Depends(get_db),
                      claims: dict = Depends(validate_token)):
    if sos_data.protocol_version not in (2, 3) or sos_data.message_type.value != 'SOS_CANCEL' or not sos_data.origin_proof:
        raise HTTPException(403, 'Cancellation requires a signed version 2 control message')
    verify_origin(sos_data.origin_proof, sos_data)
    from datetime import datetime, timezone
    age = (datetime.now(timezone.utc) - sos_data.client_timestamp).total_seconds()
    if age < -300 or age >= sos_data.ttl_seconds:
        raise HTTPException(422, 'Expired cancellation')
    response = await sos_service.cancel_sos(db, SOSCancelRequest(sos_id=sos_data.sos_id,
        user_id=sos_data.user_id, reason=sos_data.message), ws_manager)
    response.receipt = server_receipt(sos_data, 'cancelled')
    return response


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
