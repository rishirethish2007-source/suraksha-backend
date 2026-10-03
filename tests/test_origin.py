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
