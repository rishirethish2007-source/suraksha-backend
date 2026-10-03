"""Opt-in integration check. Run migrations against a disposable database first."""
import os
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.schemas.sos import SOSCreateRequest
from app.models.sos import SOSOutbox
from app.services import sos_service
from app.config import settings

@pytest.mark.asyncio
async def test_postgis_spatial_query_and_atomic_outbox(monkeypatch):
    url = os.getenv('TEST_DATABASE_URL')
    if not url: pytest.skip('Set TEST_DATABASE_URL to a migrated disposable PostGIS database')
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, 'REDIS_ENABLED', True)
    sos_id = str(uuid4())
    try:
        async with factory() as db:
            index = await db.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_sos_events_location'"))
            assert 'gist' in index.lower()
            payload = SOSCreateRequest(sos_id=sos_id,user_id='integration',user_name='Integration',sos_type='MEDICAL',location={'lat':0,'lng':179.999},client_timestamp=datetime.now(timezone.utc))
            await sos_service.process_sos(db,payload,AsyncMock())
            assert any(item.sos_id == sos_id for item in await sos_service.get_active_sos_events(db,0,-179.999,1))
            assert not any(item.sos_id == sos_id for item in await sos_service.get_active_sos_events(db,0,170,1))
            rows = (await db.execute(select(SOSOutbox))).scalars().all()
            assert any(row.payload['data']['sos_id']==sos_id for row in rows)
        redis_url = os.getenv('TEST_REDIS_URL')
        if redis_url:
            from redis.asyncio import Redis
            from app import realtime
            redis = Redis.from_url(redis_url,decode_responses=True)
            monkeypatch.setattr(realtime,'redis_client',redis)
            monkeypatch.setattr(realtime,'AsyncSessionLocal',factory)
            async with redis.pubsub() as subscriber:
                await subscriber.subscribe('suraksha:sos')
                await subscriber.get_message(timeout=2)
                task = asyncio.create_task(realtime.publisher())
                try:
                    async def receive():
                        async for message in subscriber.listen():
                            if message['type']=='message' and sos_id in message['data']: return message
                    assert await asyncio.wait_for(receive(),5)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError): await task
            await redis.aclose()
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM sos_outbox WHERE payload->'data'->>'sos_id'=:id"),{'id':sos_id})
            await db.execute(text('DELETE FROM sos_events WHERE sos_id=:id'),{'id':sos_id})
            await db.commit()
        await engine.dispose()
