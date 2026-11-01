"""CA-signed, target-bound server receipts that can travel over untrusted BLE."""
import hashlib
from datetime import datetime, timedelta, timezone
from jose import jwt
from app.config import settings
from app.device_security import ca_key


def server_receipt(event, status: str) -> str | None:
    # Legacy installations without a device CA still support their existing API.
    if not event.origin_device_id or not settings.DEVICE_CA_PRIVATE_KEY:
        return None
    ca_key()  # Validate P-256, the same trust root used for device enrollment.
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    identity = f"{event.sos_id}:{event.user_id}:{event.origin_device_id}:{event.revision}:{status}"
    packet = {
        'protocolVersion': 3, 'revision': 0,
        'sosId': 'receipt-' + hashlib.sha256(identity.encode()).hexdigest(),
        'messageType': 'SOS_ACK', 'userId': 'suraksha-server', 'userName': 'Suraksha server',
        'userPhone': '', 'sosType': event.sos_type.value, 'location': None,
        'timestamp': timestamp, 'createdAt': timestamp, 'originDeviceId': 'suraksha-server',
        'hopCount': 0, 'maxHops': 15, 'relayChain': [], 'ttlSeconds': 3600,
        'deliveryMethod': 'BLE_RELAY', 'status': 'ACTIVE',
        'targetSosId': event.sos_id, 'targetDeviceId': event.origin_device_id,
        'receiptKind': 'server', 'receiptRevision': event.revision,
        'receiptMessageType': 'SOS_CANCEL' if status == 'cancelled' else 'SOS_ALERT',
        'receiptStatus': status,
        'receiptUserId': event.user_id,
    }
    return jwt.encode({'iss': 'suraksha-device-ca', 'aud': 'suraksha-receipt',
        'iat': now, 'exp': now + timedelta(hours=1), 'packet': packet},
        settings.DEVICE_CA_PRIVATE_KEY, algorithm='ES256')
