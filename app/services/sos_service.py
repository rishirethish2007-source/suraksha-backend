"""
Business logic service for handling SOS events.
"""
from datetime import datetime, timezone
import math
from typing import List, Optional
import uuid
import aiofiles
import os

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import UploadFile, HTTPException
from app.config import settings

from app.models.sos import SOSEvent, SOSStatus, MediaAttachment, SOSCancellationRecord
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
        client_timestamp=sos.client_timestamp.replace(tzinfo=timezone.utc),
        created_at=sos.created_at.replace(tzinfo=timezone.utc),
        distance_meters=distance,
        acknowledged_by=sos.acknowledged_by,
        responders_en_route=sos.responders_en_route,
        max_hops=sos.max_hops, ttl_seconds=sos.ttl_seconds, origin_device_id=sos.origin_device_id
    )


async def process_sos(db: AsyncSession, sos_data: SOSCreateRequest, ws_manager: WebSocketManager) -> SOSResponse:
    """
    Process a new SOS alert, deduplicate, save to DB, and broadcast.
    """
    await lock_sos(db, sos_data.sos_id)
    tombstone = await db.get(SOSCancellationRecord, (sos_data.sos_id, sos_data.user_id))
    if tombstone:
        raise HTTPException(410, "SOS has been cancelled")
    # 1. Deduplication check
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_data.sos_id)
    result = await db.execute(stmt)
    existing_sos = result.scalars().first()

    if existing_sos:
        if existing_sos.user_id != sos_data.user_id:
            raise HTTPException(409, "SOS ID belongs to another user")
        return SOSResponse(
            success=True,
            sos_id=sos_data.sos_id,
            message="SOS alert received (duplicate)",
            is_duplicate=True
        )

    timestamp = sos_data.client_timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - timestamp).total_seconds()
    if age >= sos_data.ttl_seconds or age < -300:
        raise HTTPException(422, "SOS timestamp is expired or too far in the future")

    # Create new SOS record
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
        relay_chain=[node.model_dump(mode="json") for node in sos_data.relay_chain] if sos_data.relay_chain else None,
        message=sos_data.message,
        media_attachment_ids=sos_data.media_attachment_ids,
        origin_device_id=sos_data.origin_device_id,
        ttl_seconds=sos_data.ttl_seconds,
        client_timestamp=sos_data.client_timestamp.astimezone(timezone.utc).replace(tzinfo=None) # Ensure timezone naive for db if setup that way
    )

    db.add(new_sos)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(stmt)).scalars().first()
        if existing and existing.user_id == sos_data.user_id:
            return SOSResponse(success=True, sos_id=sos_data.sos_id,
                               message="SOS alert received (duplicate)", is_duplicate=True)
        raise
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
    await lock_sos(db, cancel_data.sos_id)
    stmt = select(SOSEvent).where(SOSEvent.sos_id == cancel_data.sos_id).with_for_update()
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    tombstone = await db.get(SOSCancellationRecord, (cancel_data.sos_id, cancel_data.user_id))
    if tombstone and tombstone.user_id != cancel_data.user_id:
        raise HTTPException(403, "Only the SOS owner can cancel it")
    if not sos_event:
        if not tombstone:
            db.add(SOSCancellationRecord(sos_id=cancel_data.sos_id, user_id=cancel_data.user_id))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await db.get(SOSCancellationRecord, (cancel_data.sos_id, cancel_data.user_id))
                if not existing or existing.user_id != cancel_data.user_id:
                    raise HTTPException(409, "Cancellation conflict")
        return SOSResponse(success=True, sos_id=cancel_data.sos_id, message="SOS cancellation recorded")

    if sos_event.user_id != cancel_data.user_id:
        raise HTTPException(403, "Only the SOS owner can cancel it")

    if sos_event.status == SOSStatus.CANCELLED.value:
        return SOSResponse(success=True, sos_id=cancel_data.sos_id, message="SOS already cancelled")

    if sos_event.status in {SOSStatus.RESOLVED.value, SOSStatus.EXPIRED.value}:
        raise HTTPException(409, "SOS is no longer active")
    if not tombstone:
        db.add(SOSCancellationRecord(sos_id=cancel_data.sos_id, user_id=cancel_data.user_id))
    sos_event.status = SOSStatus.CANCELLED.value
    sos_event.cancelled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    sos_event.cancellation_reason = cancel_data.reason

    await db.commit()
    await db.refresh(sos_event)
    
    # Broadcast cancellation
    response_obj = _convert_to_response(sos_event)
    await ws_manager.broadcast_sos(WebSocketSOSMessage(type="sos_cancel", data=response_obj))

    return SOSResponse(success=True, sos_id=sos_event.sos_id, message="SOS cancelled successfully")


async def get_active_sos_events(db: AsyncSession, lat: float, lng: float, radius_km: float) -> List[SOSEventResponse]:
    """
    Fetch unexpired active alerts and filter by great-circle distance.
    """
    stmt = select(SOSEvent).where(SOSEvent.status.in_([SOSStatus.ACTIVE.value, SOSStatus.ACKNOWLEDGED.value, SOSStatus.RESPONDING.value]))
    result = await db.execute(stmt)
    rows = result.scalars().all()
    
    active_events = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    for sos in rows:
        # Check TTL
        time_elapsed = (now - sos.client_timestamp).total_seconds()
        if time_elapsed >= sos.ttl_seconds:
            continue
            
        dist = distance_meters(lat, lng, sos.latitude, sos.longitude)
        if dist <= radius_km * 1000:
            resp = _convert_to_response(sos, distance=dist)
            active_events.append(resp)
        
    # Sort by distance
    active_events.sort(key=lambda x: x.distance_meters if x.distance_meters is not None else float('inf'))
    return active_events


async def acknowledge_sos(db: AsyncSession, sos_id: str, user_id: str, ws_manager: WebSocketManager) -> SOSResponse:
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id).with_for_update()
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    if not sos_event:
        return SOSResponse(success=False, sos_id=sos_id, message="SOS not found")
        
    ensure_active(sos_event)
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
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id).with_for_update()
    result = await db.execute(stmt)
    sos_event = result.scalars().first()

    if not sos_event:
        return SOSResponse(success=False, sos_id=sos_id, message="SOS not found")
        
    ensure_active(sos_event)
    responder_ids = list(sos_event.responder_ids or [])
    if user_id in responder_ids:
        return SOSResponse(success=True, sos_id=sos_id, message="Responder already en route")
    sos_event.responder_ids = [*responder_ids, user_id]
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
    stmt = select(SOSEvent).where(SOSEvent.sos_id == sos_id).with_for_update()
    res = await db.execute(stmt)
    sos_event = res.scalars().first()
    if not sos_event:
        raise ValueError(f"SOS with id {sos_id} not found")
    
    if sos_event.user_id != uploaded_by:
        raise HTTPException(403, "Only the SOS owner can attach media")
    allowed = {"image/jpeg": (".jpg", "IMAGE"), "image/png": (".png", "IMAGE"),
               "audio/mpeg": (".mp3", "AUDIO"), "audio/mp4": (".m4a", "AUDIO"),
               "video/mp4": (".mp4", "VIDEO")}
    if file.content_type not in allowed:
        raise HTTPException(415, "Unsupported media type")
    ext, file_type = allowed[file.content_type]
    os.makedirs("uploads", exist_ok=True)
    attachment_id = str(uuid.uuid4())
    file_name = f"{attachment_id}{ext}"
    file_path = f"uploads/{file_name}"
    file_size = 0
    try:
        async with aiofiles.open(file_path, "wb") as output:
            while chunk := await file.read(64 * 1024):
                file_size += len(chunk)
                if file_size > settings.MEDIA_UPLOAD_MAX_SIZE_MB * 1024 * 1024:
                    raise HTTPException(413, "Media exceeds upload size limit")
                await output.write(chunk)
        if not file_size:
            raise HTTPException(400, "Empty media file")
        attachment = MediaAttachment(
            id=attachment_id, sos_id=sos_id, file_url=f"/api/v1/sos/media/{attachment_id}",
            file_type=file_type, file_size_bytes=file_size, uploaded_by=uploaded_by,
        )
        db.add(attachment)
        sos_event.media_attachment_ids = [*(sos_event.media_attachment_ids or []), attachment_id]
        await db.commit()
    except BaseException:
        await db.rollback()
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    await db.refresh(attachment)
    return MediaUploadResponse(attachment_id=attachment_id, file_url=attachment.file_url,
                               uploaded_at=attachment.uploaded_at)


def distance_meters(lat1, lng1, lat2, lng2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


def ensure_active(event):
    if event.status not in {SOSStatus.ACTIVE.value, SOSStatus.ACKNOWLEDGED.value, SOSStatus.RESPONDING.value}:
        raise HTTPException(409, "SOS is no longer active")
    if (datetime.now(timezone.utc).replace(tzinfo=None) - event.client_timestamp).total_seconds() >= event.ttl_seconds:
        raise HTTPException(409, "SOS has expired")


async def lock_sos(db, sos_id):
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:sos_id, 0))"), {"sos_id": sos_id})
