import json
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from app.config import settings
from app.device_security import DeviceEnrollment, OriginProof, enroll, verify_origin
from app.schemas.sos import SOSCreateRequest

@pytest.fixture
def signed(monkeypatch):
    ca = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', ca.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()
    enrolled = enroll(DeviceEnrollment(device_id='device-a', public_key=public), {'sub':'phone-a'})
    stamp = datetime.now(timezone.utc).isoformat()
    fields = dict(sos_id='signed-sos', user_id='phone-a', user_name='Phone A', user_phone='', sos_type='MEDICAL', lat=12.97, lng=77.59, timestamp=stamp, origin_device_id='device-a', ttl_seconds=3600, max_hops=15, message='Help')
    raw = json.dumps(fields, separators=(',', ':'))
    r,s = utils.decode_dss_signature(key.sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
    proof = OriginProof(certificate=enrolled['certificate'], signed_payload=raw, signature=(r.to_bytes(32,'big')+s.to_bytes(32,'big')).hex())
    event = SOSCreateRequest(sos_id=fields['sos_id'],user_id='phone-a',user_name='Phone A',sos_type='MEDICAL',location={'lat':12.97,'lng':77.59},client_timestamp=stamp,origin_device_id='device-a',message='Help',origin_proof=proof)
    return event

def test_valid_signed_origin_and_relay_metadata(signed):
    verify_origin(signed.origin_proof, signed)
    signed.hop_count = 2
    verify_origin(signed.origin_proof, signed)

@pytest.mark.parametrize('field,value', [('user_id','forged'),('origin_device_id','other'),('message','forged'),('ttl_seconds',60),('max_hops',2)])
def test_tampered_origin_rejected(signed, field, value):
    setattr(signed, field, value)
    with pytest.raises(HTTPException) as error: verify_origin(signed.origin_proof, signed)
    assert error.value.status_code == 403

def test_tampered_coordinate_and_signature(signed):
    signed.location.lat += 1
    with pytest.raises(HTTPException): verify_origin(signed.origin_proof, signed)
    signed.location.lat -= 1
    signed.origin_proof.signature = '00'*64
    with pytest.raises(HTTPException): verify_origin(signed.origin_proof, signed)

def test_wrong_authority_rejected(signed, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    with pytest.raises(HTTPException): verify_origin(signed.origin_proof, signed)

def test_valid_locationless_signed_origin(monkeypatch):
    ca = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', ca.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()
    certificate = enroll(DeviceEnrollment(device_id='device', public_key=public), {'sub':'owner'})['certificate']
    stamp = datetime.now(timezone.utc).isoformat()
    fields = dict(sos_id='no-gps', user_id='owner', user_name='Owner', user_phone='', sos_type='MEDICAL',
                  lat=None, lng=None, timestamp=stamp, origin_device_id='device', ttl_seconds=3600, max_hops=15, message='')
    raw = json.dumps(fields, separators=(',', ':'))
    r, s = utils.decode_dss_signature(key.sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
    proof = OriginProof(certificate=certificate, signed_payload=raw, signature=(r.to_bytes(32,'big')+s.to_bytes(32,'big')).hex())
    event = SOSCreateRequest(sos_id='no-gps', user_id='owner', user_name='Owner', sos_type='MEDICAL',
                             location=None, client_timestamp=stamp, origin_device_id='device', origin_proof=proof)
    verify_origin(proof, event)
    event.location = SOSCreateRequest(sos_id='other', user_id='owner', user_name='Owner', sos_type='MEDICAL',
                                    location={'lat':0,'lng':0}, client_timestamp=stamp).location
    with pytest.raises(HTTPException):
        verify_origin(proof, event)

def test_legacy_signature_cannot_authorize_cancel_or_revision(signed):
    from app.models.sos import MessageType
    signed.message_type = MessageType.SOS_CANCEL
    with pytest.raises(HTTPException): verify_origin(signed.origin_proof, signed)
    signed.message_type = MessageType.SOS_ALERT
    signed.revision = 1
    with pytest.raises(HTTPException): verify_origin(signed.origin_proof, signed)


def test_v2_signed_control_binds_type_revision_and_consent(monkeypatch):
    from app.api.sos import mesh_cancel
    ca = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', ca.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = enroll(DeviceEnrollment(device_id='a', public_key=key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()), {'sub':'owner'})['certificate']
    stamp = datetime.now(timezone.utc).isoformat()
    fields = dict(protocol_version=2, message_type='SOS_CANCEL', revision=4, helper_requested=False,
        target_sos_id='', target_device_id='', sos_id='cancel', user_id='owner', user_name='Owner',
        user_phone='', sos_type='MEDICAL', lat=None, lng=None, timestamp=stamp, origin_device_id='a', ttl_seconds=3600, max_hops=15, message='')
    raw = json.dumps(fields)
    r,s = utils.decode_dss_signature(key.sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
    proof = OriginProof(certificate=certificate, signed_payload=raw, signature=(r.to_bytes(32,'big')+s.to_bytes(32,'big')).hex())
    event = SOSCreateRequest(protocol_version=2, revision=4, message_type='SOS_CANCEL', sos_id='cancel', user_id='owner', user_name='Owner', sos_type='MEDICAL', client_timestamp=stamp, origin_device_id='a', origin_proof=proof)
    verify_origin(proof, event)
    for field, value in [('revision', 5), ('helper_requested', True), ('protocol_version', None)]:
        changed = event.model_copy(update={field:value})
        with pytest.raises(HTTPException): verify_origin(proof, changed)


def test_v3_binds_capture_time_accuracy_and_request_identity(monkeypatch):
    from datetime import timedelta
    ca = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(settings, 'DEVICE_CA_PRIVATE_KEY', ca.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = enroll(DeviceEnrollment(device_id='device', public_key=key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()), {'sub':'owner'})['certificate']
    stamp = datetime.now(timezone.utc).isoformat()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    fields = dict(protocol_version=3, message_type='SOS_ALERT', revision=2, helper_requested=True,
        helper_request_id='request-1', helper_request_expires_at=expiry, helper_state='pending',
        target_sos_id='', target_device_id='', sos_id='test-v3', user_id='owner', user_name='Owner',
        user_phone='', sos_type='MEDICAL', lat=12, lng=77, timestamp=stamp, origin_device_id='device',
        ttl_seconds=3600, max_hops=15, message='', location_captured_at=stamp, location_accuracy=5,
        location_provider='gps', receipt_kind='', receipt_revision=0, receipt_message_type='', receipt_status='', receipt_user_id='')
    raw = json.dumps(fields)
    r, s = utils.decode_dss_signature(key.sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
    proof = OriginProof(certificate=certificate, signed_payload=raw, signature=(r.to_bytes(32,'big')+s.to_bytes(32,'big')).hex())
    event = SOSCreateRequest(protocol_version=3, revision=2, helper_requested=True, helper_request_id='request-1',
        helper_request_expires_at=expiry, helper_state='pending', sos_id='test-v3', user_id='owner', user_name='Owner',
        sos_type='MEDICAL', client_timestamp=stamp, origin_device_id='device', origin_proof=proof,
        location={'lat':12, 'lng':77, 'accuracy':5, 'provider':'gps', 'captured_at':stamp})
    verify_origin(proof, event)
    for field, value in [('helper_request_id', 'different'), ('helper_state', 'withdrawn'), ('helper_request_expires_at', datetime.now(timezone.utc))]:
        changed = event.model_copy(update={field:value})
        with pytest.raises(HTTPException): verify_origin(proof, changed)
    changed = event.model_copy(deep=True)
    changed.location.captured_at -= timedelta(minutes=1)
    with pytest.raises(HTTPException): verify_origin(proof, changed)
    changed = event.model_copy(deep=True)
    changed.location.accuracy = 1
    with pytest.raises(HTTPException): verify_origin(proof, changed)
    for field in ('helper_request_expires_at', 'location_captured_at', 'timestamp'):
        malformed = proof.model_copy(update={'signed_payload': json.dumps({**fields, field: {'invalid': 'timestamp'}})})
        with pytest.raises(HTTPException) as error: verify_origin(malformed, event)
        assert error.value.status_code == 403


def test_server_receipts_are_signed_and_bound_to_target_revision(signed):
    from jose import jwt, JWTError
    from app.services.receipts import server_receipt
    from app.device_security import ca_key
    pem = ca_key().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    token = server_receipt(signed, 'accepted')
    claims = jwt.decode(token, pem, algorithms=['ES256'], audience='suraksha-receipt', issuer='suraksha-device-ca')
    packet = claims['packet']
    assert packet['targetSosId'] == signed.sos_id
    assert packet['targetDeviceId'] == signed.origin_device_id
    assert packet['receiptRevision'] == signed.revision
    assert packet['receiptMessageType'] == 'SOS_ALERT'
    assert packet['receiptStatus'] == 'accepted'
    assert packet['location'] is None
    cancellation = jwt.decode(server_receipt(signed, 'cancelled'), pem, algorithms=['ES256'], audience='suraksha-receipt')
    assert cancellation['packet']['receiptStatus'] == 'cancelled'
    assert cancellation['packet']['receiptMessageType'] == 'SOS_CANCEL'
    with pytest.raises(JWTError): jwt.decode(token, pem, algorithms=['ES256'], audience='suraksha-ble')


@pytest.mark.asyncio
async def test_final_hop_upload_uses_relay_authentication(signed, monkeypatch):
    from unittest.mock import AsyncMock
    from app.api.sos import create_sos
    from app.models.sos import DeliveryMethod
    from app.schemas.sos import SOSResponse
    from app.services import sos_service
    signed.hop_count = signed.max_hops
    signed.delivery_method = DeliveryMethod.BLE_RELAY
    process = AsyncMock(return_value=SOSResponse(success=True, sos_id=signed.sos_id, message='accepted'))
    monkeypatch.setattr(sos_service, 'process_sos', process)
    response = await create_sos(signed, object(), {'sub':'final-online-relay'})
    assert response.receipt
    assert process.call_args.kwargs['actor_id'] == 'final-online-relay'
    # A zero-hop cap must still allow its first receiver to upload without
    # impersonating the origin; the origin signature is always checked.
    signed.hop_count = 0
    response = await create_sos(signed, object(), {'sub':'final-online-relay'})
    assert response.success


def test_legacy_metadata_does_not_claim_authenticated_capture_time(signed):
    wire = signed.model_dump(mode='json')
    wire['location']['captured_at'] = datetime.now(timezone.utc).isoformat()
    wire['helper_request_id'] = 'injected'
    wire['helper_state'] = 'withdrawn'
    event = SOSCreateRequest.model_validate(wire)
    assert event.location.captured_at is None
    assert event.helper_request_id is None
    assert event.helper_state is None
    verify_origin(event.origin_proof, event)
