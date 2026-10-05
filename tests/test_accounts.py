from datetime import datetime, timedelta
import hashlib
import httpx
import pytest
from fastapi import FastAPI
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.accounts import router, _attempts
from app.config import settings
from app.db.database import Base, get_db
from app.models.accounts import Account, AccountSession

@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings,'JWT_SECRET_KEY','test-only-key-'*4)
    monkeypatch.setattr(settings,'JWT_PUBLIC_KEY','')
    monkeypatch.setattr(settings,'JWT_ALGORITHM','HS256')
    monkeypatch.setattr(settings,'REDIS_ENABLED',False)
    _attempts.clear()
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        app=FastAPI();app.include_router(router)
        async def database():yield db
        app.dependency_overrides[get_db]=database
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:yield client,db
    await engine.dispose()

DATA={'email':'Person@Example.com','password':'a-strong-test-password-123','name':'Person','phone':'+911234567890'}

async def test_register_login_refresh_and_revocation(client):
    c,db=client
    r=await c.post('/api/v1/accounts/register',json=DATA)
    assert r.status_code==201 and r.headers['cache-control']=='no-store'
    data=r.json();secret=data['refresh_token']
    claims=jwt.decode(data['access_token'],settings.JWT_SECRET_KEY,algorithms=['HS256'],audience=settings.JWT_AUDIENCE,issuer=settings.JWT_ISSUER)
    assert claims['role']=='user' and claims['phone_number']==DATA['phone'] and claims['exp']-claims['iat']==900
    account=(await db.execute(select(Account))).scalar_one()
    assert account.email=='person@example.com' and DATA['password'] not in account.password_hash
    session=(await db.execute(select(AccountSession))).scalar_one()
    assert session.token_hash==hashlib.sha256(secret.encode()).hexdigest()
    # Persistent sessions are not tied to the old 24-hour test-token lifetime.
    session.created_at=datetime.now()-timedelta(days=400);await db.commit()
    assert (await c.post('/api/v1/accounts/refresh',json={'refresh_token':secret})).status_code==200
    login=await c.post('/api/v1/accounts/login',json={'email':DATA['email'].lower(),'password':DATA['password']})
    assert login.status_code==200
    await c.post('/api/v1/accounts/logout',json={'refresh_token':secret})
    assert (await c.post('/api/v1/accounts/refresh',json={'refresh_token':secret})).status_code==401
    assert (await c.post('/api/v1/accounts/refresh',json={'refresh_token':login.json()['refresh_token']})).status_code==200
    account.enabled=False;await db.commit()
    assert (await c.post('/api/v1/accounts/refresh',json={'refresh_token':login.json()['refresh_token']})).status_code==401

async def test_registration_cannot_choose_privileged_role_and_rejects_duplicates(client):
    c,db=client
    assert (await c.post('/api/v1/accounts/register',json={**DATA,'role':'admin'})).status_code==422
    assert (await c.post('/api/v1/accounts/register',json=DATA)).status_code==201
    assert (await c.post('/api/v1/accounts/register',json=DATA)).status_code==409
    assert (await c.post('/api/v1/accounts/login',json={'email':DATA['email'],'password':'incorrect-password-123'})).status_code==401

async def test_login_rate_limited(client):
    c,db=client
    for _ in range(20):
        assert (await c.post('/api/v1/accounts/refresh',json={'refresh_token':'x'*40})).status_code==401
    r=await c.post('/api/v1/accounts/refresh',json={'refresh_token':'x'*40})
    assert r.status_code==429 and r.headers['retry-after']=='60'
