"""Generate LOCAL TEST credentials; never deploy these accounts to production."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from dotenv import dotenv_values, set_key
from jose import jwt


def setup(root: Path, repair: bool = False):
    env = root / '.env'
    if env.exists() and not repair:
        raise ValueError('.env already exists. For local testing, rerun with --repair-local-auth to preserve database settings and valid signing keys.')
    values = dotenv_values(env, encoding='utf-8-sig') if env.exists() else {}
    # Environment variables override .env in the API. Refuse a repair which
    # would appear successful while the running server uses different values.
    auth_keys = ('JWT_SECRET_KEY', 'JWT_PUBLIC_KEY', 'JWT_ALGORITHM', 'JWT_ISSUER', 'JWT_AUDIENCE', 'DEVICE_CA_PRIVATE_KEY')
    for name in auth_keys:
        if name in os.environ and os.environ[name] != values.get(name):
            raise ValueError(f'{name} is overridden in this terminal. Remove that environment override before local setup.')
    if values.get('JWT_PUBLIC_KEY') or values.get('JWT_ALGORITHM', 'HS256') not in ('', 'HS256'):
        raise ValueError('An external JWT provider is configured. Refusing to replace it with local test authentication.')
    changes = {}
    secret = values.get('JWT_SECRET_KEY') or ''
    if len(secret) < 32:
        secret = secrets.token_urlsafe(48)
        changes['JWT_SECRET_KEY'] = secret
    pem = values.get('DEVICE_CA_PRIVATE_KEY')
    if not pem:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        changes['DEVICE_CA_PRIVATE_KEY'] = pem
    else:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('Existing device CA is not P-256. It was preserved; correct its configuration explicitly.')
    defaults = {'JWT_ALGORITHM': 'HS256', 'JWT_ISSUER': 'suraksha', 'JWT_AUDIENCE': 'suraksha-api'}
    for name, default in defaults.items():
        if not values.get(name): changes[name] = default
    if not env.exists(): changes['CORS_ORIGINS'] = '["http://localhost:8081"]'
    now = datetime.now(timezone.utc)
    tokens = {user: jwt.encode({'sub': user, 'name': user, 'role': 'responder',
        'iss': values.get('JWT_ISSUER') or defaults['JWT_ISSUER'],
        'aud': values.get('JWT_AUDIENCE') or defaults['JWT_AUDIENCE'],
        'exp': now + timedelta(days=1)}, secret, algorithm='HS256')
        for user in ('phone-a', 'phone-b', 'phone-c')}
    if changes:
        if env.exists():
            backup = root / ('.env.local-auth-backup-' + now.strftime('%Y%m%dT%H%M%S%f'))
            shutil.copyfile(env, backup)
            backup.chmod(0o600)
        fd, temporary = tempfile.mkstemp(prefix='.env.setup-', dir=root)
        os.close(fd)
        try:
            target = Path(temporary)
            target.write_text(env.read_text(encoding='utf-8-sig') if env.exists() else '', encoding='utf-8')
            for name, value in changes.items(): set_key(temporary, name, value, quote_mode='always')
            target.chmod(0o600)
            os.replace(temporary, env)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    token_file = root / 'dev-tokens.json'
    token_file.write_text(json.dumps(tokens, indent=2), encoding='utf-8')
    token_file.chmod(0o600)
    print('Local test authentication configured; database settings and valid signing keys preserved.')
    print('Fresh 24-hour phone tokens saved to dev-tokens.json. Restart Uvicorn, then use the phone-a token in Suraksha Test.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair-local-auth', action='store_true', help='Repair missing local auth settings while preserving existing .env values and valid keys; refresh test tokens.')
    args = parser.parse_args()
    try:
        setup(Path(__file__).resolve().parents[1], args.repair_local_auth)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
