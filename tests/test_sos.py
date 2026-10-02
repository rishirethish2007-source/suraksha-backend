import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.database import Base
from app.models.sos import SOSEvent, MediaAttachment
from app.schemas.sos import SOSCreateRequest, SOSCancelRequest
from app.services import sos_service as service
from app.config import settings
from app.auth import validate_token
from jose import jwt

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()

@pytest.fixture
def payload():
    return SOSCreateRequest(sos_id="test-sos", user_id="owner", user_name="Owner",
        sos_type="MEDICAL", location={"lat": 12, "lng": 77},
        client_timestamp=datetime.now(timezone.utc),
        relay_chain=[{"device_id": "relay", "timestamp": datetime.now(timezone.utc)}])

@pytest.mark.asyncio
async def test_relay_json_timezone_and_duplicate(db, payload):
    payload.client_timestamp = payload.client_timestamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
    ws = AsyncMock()
    assert (await service.process_sos(db, payload, ws)).success
    assert (await service.process_sos(db, payload, ws)).is_duplicate
    event = (await db.execute(select(SOSEvent))).scalar_one()
    json.dumps(event.relay_chain)
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - event.client_timestamp).total_seconds()) < 5
    assert ws.broadcast_sos.await_count == 1

@pytest.mark.asyncio
async def test_cancel_ownership_and_late_relay(db, payload):
    await service.process_sos(db, payload, AsyncMock())
    with pytest.raises(HTTPException) as error:
        await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id="stranger"), AsyncMock())
    assert error.value.status_code == 403
    await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id="owner"), AsyncMock())
    with pytest.raises(HTTPException) as error:
        await service.process_sos(db, payload, AsyncMock())
    assert error.value.status_code == 410

@pytest.mark.asyncio
async def test_cancel_before_arrival(db, payload):
    assert (await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id="owner"), AsyncMock())).success
    with pytest.raises(HTTPException):
        await service.process_sos(db, payload, AsyncMock())

@pytest.mark.asyncio
async def test_responder_idempotency_and_nearby_statuses(db, payload):
    await service.process_sos(db, payload, AsyncMock())
    await service.acknowledge_sos(db, payload.sos_id, "responder", AsyncMock())
    assert len(await service.get_active_sos_events(db, 12, 77, 1)) == 1
    await service.respond_to_sos(db, payload.sos_id, "responder", AsyncMock())
    await service.respond_to_sos(db, payload.sos_id, "responder", AsyncMock())
    events = await service.get_active_sos_events(db, 12, 77, 1)
    assert events[0].responders_en_route == 1
    await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id="owner"), AsyncMock())
    with pytest.raises(HTTPException):
        await service.respond_to_sos(db, payload.sos_id, "other", AsyncMock())

@pytest.mark.asyncio
async def test_expiry_uses_origin_timestamp(db, payload):
    payload.client_timestamp -= timedelta(minutes=59)
    await service.process_sos(db, payload, AsyncMock())
    event = (await db.execute(select(SOSEvent))).scalar_one()
    event.client_timestamp -= timedelta(minutes=2)
    await db.commit()
    assert await service.get_active_sos_events(db, 12, 77, 100) == []

@pytest.mark.asyncio
async def test_media_id_and_limit_cleanup(db, payload, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await service.process_sos(db, payload, AsyncMock())
    def upload(data):
        return UploadFile(filename="unsafe.html", file=io.BytesIO(data), headers=Headers({"content-type": "image/jpeg"}))
    result = await service.save_media(db, payload.sos_id, upload(b"image"), "owner")
    event = (await db.execute(select(SOSEvent))).scalar_one()
    assert result.attachment_id in event.media_attachment_ids
    assert "None" not in event.media_attachment_ids
    monkeypatch.setattr(settings, "MEDIA_UPLOAD_MAX_SIZE_MB", 0)
    with pytest.raises(HTTPException) as error:
        await service.save_media(db, payload.sos_id, upload(b"too large"), "owner")
    assert error.value.status_code == 413
    assert len(list((tmp_path / "uploads").iterdir())) == 1
    assert len((await db.execute(select(MediaAttachment))).scalars().all()) == 1

@pytest.mark.parametrize("lat1,lng1,lat2,lng2,maximum", [(0,179.9,0,-179.9,23000), (89,0,89,90,160000)])
def test_geographic_distance(lat1,lng1,lat2,lng2,maximum):
    assert 0 < service.distance_meters(lat1,lng1,lat2,lng2) < maximum

@pytest.mark.parametrize("changes", [{"ttl_seconds":0}, {"hop_count":16}, {"max_hops":-1}, {"client_timestamp":"2026-10-02T12:00:00"}])
def test_schema_limits(payload, changes):
    with pytest.raises(ValueError):
        SOSCreateRequest.model_validate({**payload.model_dump(), **changes})

def test_jwt(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret-" * 4)
    claims = {"sub":"owner", "exp":datetime.now(timezone.utc)+timedelta(minutes=5), "iss":settings.JWT_ISSUER, "aud":settings.JWT_AUDIENCE}
    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")
    assert validate_token(f"Bearer {token}")["sub"] == "owner"
    for invalid in [None, "Bearer fake", f"Bearer {token}x"]:
        with pytest.raises(HTTPException): validate_token(invalid)


@pytest.mark.asyncio
async def test_api_auth_and_subject_checks(db, payload, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.db.database import get_db
    from app.api.sos import _rate_limits
    async def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    _rate_limits.clear()
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret-" * 4)
    claims = {"sub":"owner", "exp":datetime.now(timezone.utc)+timedelta(minutes=5), "iss":settings.JWT_ISSUER, "aud":settings.JWT_AUDIENCE}
    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = payload.model_dump(mode="json")
            assert (await client.post("/api/v1/sos", json=body)).status_code == 401
            body["hop_count"] = 1
            assert (await client.post("/api/v1/sos", json=body)).status_code == 401
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.post("/api/v1/sos", json=body, headers=headers)).status_code == 403
            body["hop_count"] = 0
            body["user_id"] = "someone-else"
            assert (await client.post("/api/v1/sos", json=body, headers=headers)).status_code == 403
            body["user_id"] = "owner"
            response = await client.post("/api/v1/sos", json=body, headers=headers)
            assert response.status_code == 200
            assert response.json()["success"] is True
            assert response.json()["server_timestamp"].endswith("Z")
            assert (await client.get("/api/v1/sos/active?lat=12&lng=77", headers=headers)).status_code == 403
    finally:
        app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_websocket_snapshot_survives_connections_changing():
    from app.websocket.manager import WebSocketManager
    manager = WebSocketManager()
    second = AsyncMock()
    first = AsyncMock()
    async def send(_):
        manager.active_connections["second"] = second
    first.send_text.side_effect = send
    manager.active_connections["first"] = first
    message = AsyncMock()
    message.model_dump_json = lambda: "{}"
    await manager.broadcast_sos(message)
    manager.disconnect("second", first)
    assert manager.active_connections["second"] is second
    await manager.close()


@pytest.mark.asyncio
async def test_foreign_tombstone_cannot_block_owner(db, payload):
    await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id="stranger"), AsyncMock())
    assert (await service.process_sos(db, payload, AsyncMock())).success
