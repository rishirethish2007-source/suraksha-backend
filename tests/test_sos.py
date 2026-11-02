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

@pytest.mark.asyncio
async def test_locationless_sos_is_persisted_and_broadcast_without_fake_coordinates(db, payload):
    payload.location = None
    ws = AsyncMock()
    assert (await service.process_sos(db, payload, ws)).success
    event = (await db.execute(select(SOSEvent))).scalar_one()
    assert event.latitude is None and event.longitude is None
    assert service._convert_to_response(event).location is None
    assert ws.broadcast_sos.call_args.args[0].data.location is None
    assert await service.get_active_sos_events(db, 12, 77, 100) == []
    assert (await service.process_sos(db, payload, ws)).is_duplicate
    assert (await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id='owner'), ws)).success

@pytest.mark.asyncio
async def test_location_revisions_preserve_status_and_ignore_late_updates(db, payload):
    payload.origin_device_id = 'device-a'
    await service.process_sos(db, payload, AsyncMock())
    await service.respond_to_sos(db, payload.sos_id, 'helper', AsyncMock())
    update = payload.model_copy(deep=True)
    update.revision = 2
    update.location.lat = 13
    await service.process_sos(db, update, AsyncMock())
    event = (await db.execute(select(SOSEvent))).scalar_one()
    assert event.latitude == 13 and event.revision == 2
    assert event.status == 'RESPONDING' and event.responders_en_route == 1
    assert (await service.process_sos(db, payload, AsyncMock())).is_duplicate
    assert event.latitude == 13
    update.revision = 3
    update.location = None
    await service.process_sos(db, update, AsyncMock())
    assert event.latitude == 13
    await service.cancel_sos(db, SOSCancelRequest(sos_id=payload.sos_id, user_id='owner'), AsyncMock())
    update.revision = 4
    with pytest.raises(HTTPException) as error:
        await service.process_sos(db, update, AsyncMock())
    assert error.value.status_code == 410

@pytest.mark.asyncio
async def test_revision_cannot_be_written_by_another_device(db, payload):
    payload.origin_device_id = 'device-a'
    await service.process_sos(db, payload, AsyncMock())
    payload.revision = 1
    payload.origin_device_id = 'device-b'
    with pytest.raises(HTTPException) as error:
        await service.process_sos(db, payload, AsyncMock())
    assert error.value.status_code == 403

@pytest.mark.asyncio
async def test_heartbeat_keeps_the_original_gps_capture_time(db, payload):
    captured = datetime.now(timezone.utc) - timedelta(minutes=9)
    payload.location.captured_at = captured
    await service.process_sos(db, payload, AsyncMock())
    update = payload.model_copy(update={'revision': 1, 'location': None, 'client_timestamp': datetime.now(timezone.utc)})
    await service.process_sos(db, update, AsyncMock())
    event = (await db.execute(select(SOSEvent))).scalar_one()
    response = service._convert_to_response(event)
    assert response.location.captured_at == captured
    assert response.client_timestamp > response.location.captured_at
    assert response.location.lat == payload.location.lat
    older = payload.model_copy(deep=True)
    older.revision = 2
    older.location.lat = 20
    older.location.captured_at = captured - timedelta(minutes=1)
    await service.process_sos(db, older, AsyncMock())
    assert (await service.get_active_sos_events(db, 12, 77, 50))[0].location.lat == 12

@pytest.mark.asyncio
async def test_busy_relay_and_duplicates_do_not_use_new_sos_budget(db, payload, monkeypatch):
    from app.services.rate_limit import _windows
    _windows.clear()
    monkeypatch.setattr(settings, 'REDIS_ENABLED', False)
    monkeypatch.setattr(settings, 'SOS_RATE_LIMIT_PER_MINUTE', 1)
    for number in range(8):
        event = payload.model_copy(update={'sos_id': f'busy-{number}', 'user_id': f'owner-{number}', 'hop_count': 1})
        await service.process_sos(db, event, AsyncMock(), actor_id='one-online-relay')
        for _ in range(3):
            assert (await service.process_sos(db, event, AsyncMock(), actor_id='one-online-relay')).is_duplicate
    assert _windows['relay:one-online-relay'][1] == 8
    first = payload.model_copy(update={'sos_id': 'busy-0', 'user_id': 'owner-0', 'revision': 1})
    await service.process_sos(db, first, AsyncMock(), actor_id='owner-0')
    assert _windows['origin:owner-0'][1] == 1
    assert _windows['update:owner-0'][1] == 1
    _windows.clear()

@pytest.mark.asyncio
async def test_relay_limit_returns_retry_after_without_charging_origin(db, payload, monkeypatch):
    from app.services.rate_limit import _windows
    _windows.clear()
    monkeypatch.setattr(settings, 'REDIS_ENABLED', False)
    monkeypatch.setattr(settings, 'SOS_RELAY_RATE_LIMIT_PER_MINUTE', 1)
    first = payload.model_copy(update={'hop_count': 1})
    await service.process_sos(db, first, AsyncMock(), actor_id='relay')
    second = payload.model_copy(update={'sos_id': 'second', 'user_id': 'second-owner', 'hop_count': 1})
    with pytest.raises(HTTPException) as error:
        await service.process_sos(db, second, AsyncMock(), actor_id='relay')
    assert error.value.status_code == 429
    assert 1 <= int(error.value.headers['Retry-After']) <= 60
    assert 'origin:second-owner' not in _windows
    _windows.clear()

@pytest.mark.asyncio
async def test_unknown_or_less_accurate_capture_cannot_replace_known_fix(db, payload):
    captured = datetime.now(timezone.utc) - timedelta(seconds=5)
    payload.location.captured_at = captured
    payload.location.accuracy = 5
    await service.process_sos(db, payload, AsyncMock())
    for revision, time, accuracy in [(1, None, 1), (2, captured, 100), (3, captured - timedelta(seconds=1), 1)]:
        update = payload.model_copy(deep=True)
        update.revision = revision
        update.location.lat = 20
        update.location.captured_at = time
        update.location.accuracy = accuracy
        await service.process_sos(db, update, AsyncMock())
        event = (await db.execute(select(SOSEvent))).scalar_one()
        assert event.latitude == 12 and event.location_accuracy == 5
    update.revision = 4
    update.location.captured_at = captured + timedelta(seconds=1)
    await service.process_sos(db, update, AsyncMock())
    assert event.latitude == 20

@pytest.mark.asyncio
async def test_websocket_receives_and_disconnects_without_runtime_error(monkeypatch):
    from app.api import sos
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(sos, 'validate_token', lambda _: {'sub': 'helper', 'role': 'responder', 'exp': 9999999999})
    socket = AsyncMock()
    socket.headers = {}
    socket.receive_text.side_effect = WebSocketDisconnect()
    await sos.websocket_endpoint(socket, 'regression-client')
    socket.accept.assert_awaited_once()
    socket.receive_text.assert_awaited_once()
    assert 'regression-client' not in sos.ws_manager.active_connections

@pytest.mark.asyncio
@pytest.mark.parametrize('location', [None, {'lat': 12.0, 'lng': 77.0, 'accuracy': 8.0, 'provider': 'gps'}])
async def test_signed_mobile_v3_http_contract_and_delayed_cancellation(db, monkeypatch, location):
    from httpx import AsyncClient, ASGITransport
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from app.main import app
    from app.db.database import get_db
    from app.device_security import DeviceEnrollment, enroll
    from app.api.sos import _rate_limits
    ca = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', ca.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    monkeypatch.setattr(settings, 'JWT_SECRET_KEY', 'test-secret-' * 4)
    _rate_limits.clear()
    device = ec.generate_private_key(ec.SECP256R1())
    public = device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()
    certificate = enroll(DeviceEnrollment(device_id='phone-a', public_key=public), {'sub': 'owner'})['certificate']
    stamp = datetime.now(timezone.utc).isoformat()
    if location: location = {**location, 'captured_at': stamp}
    body = {'protocol_version': 3, 'revision': 0, 'sos_id': 'http-v3', 'user_id': 'owner', 'user_name': 'Owner',
        'user_phone': '', 'sos_type': 'MEDICAL', 'message_type': 'SOS_ALERT', 'location': location,
        'origin_device_id': 'phone-a', 'ttl_seconds': 3600, 'max_hops': 15, 'hop_count': 3,
        'relay_chain': [{'device_id': name, 'timestamp': stamp} for name in ['b', 'c', 'd']],
        'delivery_method': 'BLE_RELAY', 'client_timestamp': stamp}
    def sign(packet):
        fix = packet['location'] or {}
        fields = dict(protocol_version=3, message_type=packet['message_type'], revision=packet['revision'],
            helper_requested=False, target_sos_id='', target_device_id='', helper_request_id='',
            helper_request_expires_at='', helper_state='', location_captured_at=fix.get('captured_at', ''),
            location_accuracy=fix.get('accuracy'), location_provider=fix.get('provider', ''),
            receipt_kind='', receipt_revision=0, receipt_message_type='', receipt_status='', receipt_user_id='',
            sos_id=packet['sos_id'], user_id='owner', user_name='Owner', user_phone='', sos_type='MEDICAL',
            lat=fix.get('lat'), lng=fix.get('lng'), timestamp=stamp, origin_device_id='phone-a',
            ttl_seconds=3600, max_hops=15, message='')
        raw = json.dumps(fields, separators=(',', ':'))
        r, s = utils.decode_dss_signature(device.sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
        return {**packet, 'origin_proof': {'certificate': certificate, 'signed_payload': raw,
            'signature': (r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).hex()}}
    async def override_db(): yield db
    app.dependency_overrides[get_db] = override_db
    token = jwt.encode({'sub': 'relay-d', 'exp': datetime.now(timezone.utc) + timedelta(minutes=5),
        'iss': settings.JWT_ISSUER, 'aud': settings.JWT_AUDIENCE}, settings.JWT_SECRET_KEY, algorithm='HS256')
    headers = {'Authorization': f'Bearer {token}'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            signed = sign(body)
            response = await client.post('/api/v1/sos', json=signed, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()['receipt']
            assert (await client.post('/api/v1/sos', json=signed, headers=headers)).json()['is_duplicate']
            invalid = {**signed, 'location': {'latitude': 12, 'longitude': 77}}
            response = await client.post('/api/v1/sos', json=invalid, headers=headers)
            assert response.status_code == 422
            assert any(error['loc'][-1] == 'lat' for error in response.json()['detail'])
            cancellation = sign({**body, 'revision': 1, 'message_type': 'SOS_CANCEL', 'location': None})
            for _ in range(2):
                response = await client.post('/api/v1/sos/mesh-cancel', json=cancellation, headers=headers)
                assert response.status_code == 200, response.text
            assert (await client.post('/api/v1/sos', json=signed, headers=headers)).status_code == 410
    finally:
        app.dependency_overrides.clear()
