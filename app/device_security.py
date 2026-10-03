"""Device certificates and immutable signed origin payloads (P-256/SHA-256)."""
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from jose import jwt, JWTError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.exceptions import InvalidSignature
from app.auth import validate_token
from app.config import settings

router = APIRouter(prefix="/api/v1", tags=["Identity"])

class DeviceEnrollment(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    public_key: str = Field(pattern=r"^04[0-9a-f]{128}$")

class OriginProof(BaseModel):
    certificate: str = Field(max_length=4096)
    signed_payload: str = Field(max_length=8192)
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

def ca_key():
    if not settings.DEVICE_CA_PRIVATE_KEY:
        raise HTTPException(503, "Device certificate authority is not configured")
    key = serialization.load_pem_private_key(settings.DEVICE_CA_PRIVATE_KEY.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise HTTPException(503, "Device certificate authority must use P-256")
    return key

@router.get("/identity/me")
def identity(claims: dict = Depends(validate_token)):
    return {"user_id": claims["sub"], "name": claims.get("name", claims["sub"]),
            "phone": claims.get("phone_number", ""), "role": claims.get("role", "user")}

@router.post("/devices/enroll")
def enroll(body: DeviceEnrollment, claims: dict = Depends(validate_token)):
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(body.public_key))
    except ValueError:
        raise HTTPException(422, "Invalid P-256 public key") from None
    key = ca_key()
    now = datetime.now(timezone.utc)
    certificate = jwt.encode({"sub": claims["sub"], "device_id": body.device_id,
        "public_key": body.public_key, "iss": "suraksha-device-ca", "aud": "suraksha-ble",
        "iat": now, "exp": now + timedelta(days=settings.DEVICE_CERT_DAYS)},
        settings.DEVICE_CA_PRIVATE_KEY, algorithm="ES256")
    public_key = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()
    return {"certificate": certificate, "ca_public_key": public_key}

def verify_origin(proof: OriginProof, event):
    try:
        pem = ca_key().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        claims = jwt.decode(proof.certificate, pem, algorithms=["ES256"], issuer="suraksha-device-ca",
            audience="suraksha-ble", options={"require_exp": True, "require_sub": True})
        signed = json.loads(proof.signed_payload)
        if not isinstance(signed, dict):
            raise ValueError("Signed payload must be an object")
        expected = {"sos_id": event.sos_id, "user_id": event.user_id, "user_name": event.user_name,
            "user_phone": event.user_phone or "", "sos_type": event.sos_type.value,
            "lat": event.location.lat, "lng": event.location.lng,
            "origin_device_id": event.origin_device_id, "ttl_seconds": event.ttl_seconds,
            "max_hops": event.max_hops, "message": event.message or ""}
        if any(signed.get(key) != value for key, value in expected.items()):
            raise ValueError("Signed fields do not match the submitted alert")
        timestamp = datetime.fromisoformat(signed["timestamp"].replace("Z", "+00:00"))
        if timestamp != event.client_timestamp or claims["sub"] != event.user_id or claims["device_id"] != event.origin_device_id:
            raise ValueError("Origin identity or timestamp mismatch")
        raw = bytes.fromhex(proof.signature)
        signature = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(claims["public_key"]))
        key.verify(signature, proof.signed_payload.encode(), ec.ECDSA(hashes.SHA256()))
    except (JWTError, ValueError, KeyError, TypeError, InvalidSignature):
        raise HTTPException(403, "Invalid or expired SOS origin proof") from None
