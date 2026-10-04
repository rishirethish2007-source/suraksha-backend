from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.dashboard import router
from app.auth import validate_token
from app.db.database import Base, get_db
from app.models.sos import SOSEvent

@pytest.fixture
async def desk():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        app = FastAPI(); app.include_router(router)
        async def database(): yield db
        app.dependency_overrides[get_db] = database
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield app, db, client
    await engine.dispose()

def event(id, status='ACTIVE', age=0):
    return SOSEvent(sos_id=id, user_id='phone-a', user_name='<script>unsafe</script>', user_phone='+911234567890',
                    sos_type='MEDICAL', message_type='SOS_ALERT', status=status, delivery_method='BLE_RELAY',
                    latitude=31.77, longitude=76.98, location_accuracy=8, message='Need assistance', hop_count=2,
                    client_timestamp=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=age),
                    ttl_seconds=3600, responders_en_route=0)

async def test_dashboard_shell_has_no_private_data_and_safe_assets(desk):
    app, db, client = desk
    r = await client.get('/dashboard')
    assert r.status_code == 200 and 'Responder desk' in r.text
    assert r.headers['cache-control'] == 'no-store'
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert (await client.get('/dashboard/app.js')).status_code == 200
    assert (await client.get('/dashboard/style.css')).status_code == 200
    assert (await client.get('/dashboard/../../.env')).status_code == 404

async def test_dashboard_requires_responder(desk):
    app, db, client = desk
    assert (await client.get('/api/v1/dashboard/events')).status_code == 401
    app.dependency_overrides[validate_token] = lambda: {'sub':'citizen','role':'citizen'}
    assert (await client.get('/api/v1/dashboard/events')).status_code == 403

async def test_dashboard_returns_details_and_filters_expired_cancelled(desk):
    app, db, client = desk
    app.dependency_overrides[validate_token] = lambda: {'sub':'desk','role':'responder'}
    db.add_all([event('active'), event('responding','RESPONDING'), event('ack','ACKNOWLEDGED'), event('old',age=3700), event('cancelled','CANCELLED')]); await db.commit()
    r = await client.get('/api/v1/dashboard/events?limit=2')
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    first = r.json(); assert len(first['events']) == 2 and first['next_offset'] == 2
    second = (await client.get('/api/v1/dashboard/events?limit=2&offset=2')).json()
    all_events = first['events'] + second['events']
    assert {e['sos_id'] for e in all_events} == {'active','responding','ack'}
    assert second['next_offset'] is None
    assert all_events[0]['location']['lat'] == 31.77
    assert all_events[0]['location']['accuracy'] == 8
    assert all_events[0]['user_phone'] == '+911234567890'
    assert all_events[0]['message'] == 'Need assistance'
    assert (await client.get('/api/v1/dashboard/events?limit=1000')).status_code == 422
