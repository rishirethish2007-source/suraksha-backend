"""Generate LOCAL TEST credentials; never deploy these accounts to production."""
from pathlib import Path
import json
import secrets
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from jose import jwt

root = Path(__file__).resolve().parents[1]
if (root / ".env").exists():
    raise SystemExit(".env already exists; refusing to overwrite credentials")
secret = secrets.token_urlsafe(48)
key = ec.generate_private_key(ec.SECP256R1())
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
(root / ".env").write_text('JWT_SECRET_KEY=' + secret + '\nDEVICE_CA_PRIVATE_KEY=' + "'" + pem + "'\n" + 'CORS_ORIGINS=["http://localhost:8081"]\n')
(root / ".env").chmod(0o600)
now = datetime.now(timezone.utc)
tokens = {}
for user in ("phone-a", "phone-b", "phone-c"):
    tokens[user] = jwt.encode({"sub":user, "name":user, "role":"responder", "iss":"suraksha", "aud":"suraksha-api", "exp":now+timedelta(days=1)}, secret, algorithm="HS256")
(root / "dev-tokens.json").write_text(json.dumps(tokens, indent=2))
(root / "dev-tokens.json").chmod(0o600)
print("Created .env and dev-tokens.json (24-hour local test tokens). Do not commit them.")
