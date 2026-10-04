from dotenv import dotenv_values
from jose import jwt
import json
import pytest
from scripts.dev_setup import setup

@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    for key in ('JWT_SECRET_KEY','JWT_PUBLIC_KEY','JWT_ALGORITHM','JWT_ISSUER','JWT_AUDIENCE','DEVICE_CA_PRIVATE_KEY'):
        monkeypatch.delenv(key, raising=False)


def test_repair_preserves_database_settings_and_stable_keys(tmp_path):
    original = 'DATABASE_URL=postgresql+asyncpg://local/test\nJWT_SECRET_KEY=short\nJWT_ISSUER=my-test\nJWT_AUDIENCE=my-api\n'
    (tmp_path/'.env').write_text(original)
    setup(tmp_path, repair=True)
    values = dotenv_values(tmp_path/'.env')
    assert values['DATABASE_URL'] == 'postgresql+asyncpg://local/test'
    assert len(values['JWT_SECRET_KEY']) >= 32
    token = json.loads((tmp_path/'dev-tokens.json').read_text())['phone-a']
    assert jwt.decode(token, values['JWT_SECRET_KEY'], algorithms=['HS256'], issuer='my-test', audience='my-api')['sub'] == 'phone-a'
    assert next(tmp_path.glob('.env.local-auth-backup-*')).read_text() == original
    setup(tmp_path, repair=True)
    assert dotenv_values(tmp_path/'.env') == values


def test_refuses_implicit_overwrite_and_external_provider(tmp_path):
    content = 'JWT_PUBLIC_KEY=external-key\n'
    (tmp_path/'.env').write_text(content)
    with pytest.raises(ValueError, match='already exists'): setup(tmp_path)
    with pytest.raises(ValueError, match='external JWT'): setup(tmp_path, repair=True)
    assert (tmp_path/'.env').read_text() == content


def test_new_setup_and_environment_override(tmp_path, monkeypatch):
    setup(tmp_path)
    assert len(json.loads((tmp_path/'dev-tokens.json').read_text())) == 3
    before = (tmp_path/'.env').read_text()
    monkeypatch.setenv('JWT_SECRET_KEY','overridden')
    with pytest.raises(ValueError, match='overridden'): setup(tmp_path, repair=True)
    assert (tmp_path/'.env').read_text() == before
