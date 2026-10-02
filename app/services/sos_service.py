"""
Business logic service for handling SOS events.
"""
from datetime import datetime, timezone
import json
from typing import List, Optional
import uuid
import aiofiles
import os

from sqlalchemy import select, update, text, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import UploadFile

from app.models.sos import SOSEvent, SOSStatus, MediaAttachment
from app.schemas.sos import (
    SOSCreateRequest, SOSCancelRequest, SOSResponse, 
    SOSEventResponse, WebSocketSOSMessage, MediaUploadResponse, SOSLocationSchema
)
from app.websocket.manager import WebSocketManager

def _convert_to_response(sos: SOSEvent, distance: Optional[float] = None) -> SOSEventResponse:
    """
    Helper to convert SOSEvent DB model to response schema.
    """
    return SOSEventResponse(
        id=str(sos.id),
        sos_id=sos.sos_id,
        user_id=sos.user_id,
        user_name=sos.user_name,
        user_phone=sos.user_phone,
        sos_type=sos.sos_type,
        message_type=sos.message_type,
        status=sos.status,
        delivery_method=sos.delivery_method,
        location=SOSLocationSchema(lat=sos.latitude, lng=sos.longitude, altitude=sos.altitude, accuracy=sos.location_accuracy, provider=sos.location_provider),
        hop_count=sos.hop_count,
        relay_chain=sos.relay_chain,
        message=sos.message,
        media_attachment_ids=sos.media_attachment_ids,
        client_timestamp=sos.client_timestamp,
        created_at=sos.created_at,
        distance_meters=distance,
        acknowledged_by=sos.acknowledged_by,
        responders_en_route=sos.responders_en_route
    )


async def process_sos(db: AsyncSession, sos_data: SOSCreateRequest, ws_manager: WebSocketManager) -> SOSResponse:
    """
    Process a new SOS alert, deduplicate, save to DB, and broadcast.
    """
    # 1. Deduplication check
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_data.sos_id)
    result = await db.execute(stmt)
    existing_sos = result.scalars().first()

    if existing_sos:
        return SOSResponse(
            success=True,
            sos_id=sos_data.sos_id,
            message="SOS alert received (duplicate)",
            is_duplicate=True
        )

    # 3. Create new SOS record
    new_sos = SOSEvent(
        sos_id=sos_data.sos_id,
        user_id=sos_data.user_id,
        user_name=sos_data.user_name,
        user_phone=sos_data.user_phone,
        sos_type=sos_data.sos_type,
        message_type=sos_data.message_type,
        latitude=sos_data.location.lat,
        longitude=sos_data.location.lng,
        altitude=sos_data.location.altitude,
        location_accuracy=sos_data.location.accuracy,
        location_provider=sos_data.location.provider,
        delivery_method=sos_data.delivery_method,
        hop_count=sos_data.hop_count,
        max_hops=sos_data.max_hops,
        relay_chain=[node.model_dump() for node in sos_data.relay_chain] if sos_data.relay_chain else None,
        message=sos_data.message,
        media_attachment_ids=sos_data.media_attachment_ids,
        origin_device_id=sos_data.origin_device_id,
        ttl_seconds=sos_data.ttl_seconds,
        client_timestamp=sos_data.client_timestamp.replace(tzinfo=None) # Ensure timezone naive for db if setup that way
    )

    db.add(new_sos)
    await db.commit()
    await db.refresh(new_sos)
    
    response_obj = _convert_to_response(new_sos)
    
    # 4. Broadcast via WebSocket
    await ws_manager.broadcast_sos(WebSocketSOSMessage(type="new_sos", data=response_obj))

    return SOSResponse(
        success=True,
        sos_id=new_sos.sos_id,
        message="SOS alert successfully processed",
        is_duplicate=False
    )


async def cancel_sos(db: AsyncSession, cancel_data: SOSCancelRequest, ws_manager: WebSocketManager) -> SOSResponse:
    """
    Cancel an active SOS event.
    """
    stmt = select(SOSEvent).where(SOSEvent.sos_id == cancel_data.sos_id)
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    if not sos_event:
        return SOSResponse(success=False, sos_id=cancel_data.sos_id, message="SOS not found")

    if sos_event.status == SOSStatus.CANCELLED.value:
        return SOSResponse(success=True, sos_id=cancel_data.sos_id, message="SOS already cancelled")

    sos_event.status = SOSStatus.CANCELLED.value
    sos_event.cancelled_at = datetime.utcnow()
    sos_event.cancellation_reason = cancel_data.reason

    await db.commit()
    await db.refresh(sos_event)
    
    # Broadcast cancellation
    response_obj = _convert_to_response(sos_event)
    await ws_manager.broadcast_sos(WebSocketSOSMessage(type="sos_cancel", data=response_obj))

    return SOSResponse(success=True, sos_id=sos_event.sos_id, message="SOS cancelled successfully")


async def get_active_sos_events(db: AsyncSession, lat: float, lng: float, radius_km: float) -> List[SOSEventResponse]:
    """
    Fetch active SOS events (SQLite compatible, no distance filtering here).
    """
    stmt = select(SOSEvent).where(SOSEvent.status == SOSStatus.ACTIVE.value)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    
    active_events = []
    now = datetime.utcnow()
    
    for sos in rows:
        # Check TTL
        time_elapsed = (now - sos.created_at).total_seconds()
        if time_elapsed > sos.ttl_seconds:
            continue
            
        # Basic euclidean distance sort
        dist = ((sos.latitude - lat)**2 + (sos.longitude - lng)**2)**0.5 * 111000  # approx meters
        if dist <= radius_km * 1000:
            resp = _convert_to_response(sos, distance=dist)
            active_events.append(resp)
        
    # Sort by distance
    active_events.sort(key=lambda x: x.distance_meters if x.distance_meters is not None else float('inf'))
    return active_events


async def acknowledge_sos(db: AsyncSession, sos_id: str, user_id: str, ws_manager: WebSocketManager) -> SOSResponse:
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id)
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    if not sos_event:
        return SOSResponse(success=False, sos_id=sos_id, message="SOS not found")
        
    if sos_event.acknowledged_by is None:
        sos_event.acknowledged_by = []
        
    if user_id not in sos_event.acknowledged_by:
        new_list = list(sos_event.acknowledged_by)
        new_list.append(user_id)
        sos_event.acknowledged_by = new_list
        if sos_event.status == SOSStatus.ACTIVE.value:
            sos_event.status = SOSStatus.ACKNOWLEDGED.value
            
        await db.commit()
        await db.refresh(sos_event)
        
        # Broadcast update
        response_obj = _convert_to_response(sos_event)
        await ws_manager.broadcast_sos(WebSocketSOSMessage(type="sos_update", data=response_obj))

    return SOSResponse(success=True, sos_id=sos_id, message="SOS acknowledged")


async def respond_to_sos(db: AsyncSession, sos_id: str, user_id: str, ws_manager: WebSocketManager) -> SOSResponse:
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id)
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    if not sos_event:
        return SOSResponse(success=False, sos_id=sos_id, message="SOS not found")
        
    sos_event.responders_en_route += 1
    if sos_event.status in [SOSStatus.ACTIVE.value, SOSStatus.ACKNOWLEDGED.value]:
        sos_event.status = SOSStatus.RESPONDING.value
        
    await db.commit()
    await db.refresh(sos_event)
    
    # Broadcast update
    response_obj = _convert_to_response(sos_event)
    await ws_manager.broadcast_sos(WebSocketSOSMessage(type="sos_update", data=response_obj))

    return SOSResponse(success=True, sos_id=sos_id, message="Responder en route marked")


async def save_media(db: AsyncSession, sos_id: str, file: UploadFile, uploaded_by: str) -> MediaUploadResponse:
    """
    Save uploaded media and create a database record.
    """
    # Verify SOS exists
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id)
    res = await db.execute(stmt)
    sos_event = res.scalars().first()
    if not sos_event:
        raise ValueError(f"SOS with id {sos_id} not found")
    
    # Ensure directory exists
    os.makedirs("uploads", exist_ok=True)
    
    file_ext = os.path.splitext(file.filename)[1] if file.filename else ""
    file_name = f"{uuid.uuid4()}{file_ext}"
    file_path = f"uploads/{file_name}"
    file_url = f"/static/{file_name}" # Path to be served by FastAPI StaticFiles
    
    # Read file data
    content = await file.read()
    file_size = len(content)
    
    # Write to disk asynchronously
    async with aiofiles.open(file_path, 'wb') as out_file:
        await out_file.write(content)
        
    # Determine file type
    file_type = "IMAGE"
    if file.content_type:
        if file.content_type.startswith("video"):
            file_type = "VIDEO"
        elif file.content_type.startswith("audio"):
            file_type = "AUDIO"

    # Save record to db
    attachment = MediaAttachment(
        sos_id=sos_id,
        file_url=file_url,
        file_type=file_type,
        file_size_bytes=file_size,
        uploaded_by=uploaded_by
    )
    db.add(attachment)
    
    # Update sos_event media_attachment_ids
    if sos_event.media_attachment_ids is None:
        sos_event.media_attachment_ids = []
    
    new_ids = list(sos_event.media_attachment_ids)
    new_ids.append(str(attachment.id))
    sos_event.media_attachment_ids = new_ids
    
    await db.commit()
    await db.refresh(attachment)
    
    return MediaUploadResponse(
        attachment_id=str(attachment.id),
        file_url=attachment.file_url,
        uploaded_at=attachment.uploaded_at
    )
