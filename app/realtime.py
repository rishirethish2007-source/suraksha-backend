"""At-least-once Redis fanout; WebGIS upserts events by sos_id."""
import asyncio
import logging
from redis.asyncio import Redis
from sqlalchemy import select
from app.config import settings
from app.db.database import AsyncSessionLocal
from app.models.sos import SOSOutbox
from app.schemas.sos import WebSocketSOSMessage
from app.websocket.manager import ws_manager

redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
_tasks = []
logger = logging.getLogger(__name__)

async def publisher():
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(SOSOutbox).order_by(SOSOutbox.created_at).limit(100).with_for_update(skip_locked=True))).scalars().all()
                for row in rows:
                    import json
                    await redis_client.publish("suraksha:sos", json.dumps(row.payload))
                    await db.delete(row)
                await db.commit()
        except Exception:
            logger.exception("Outbox delivery failed; pending events retained")
        await asyncio.sleep(1)

async def subscriber():
    while True:
        try:
            async with redis_client.pubsub() as channel:
                await channel.subscribe("suraksha:sos")
                async for message in channel.listen():
                    if message["type"] == "message":
                        await ws_manager.broadcast_sos(WebSocketSOSMessage.model_validate_json(message["data"]))
        except Exception:
            logger.exception("Redis subscriber reconnecting")
            await asyncio.sleep(2)

async def start_realtime():
    if settings.REDIS_ENABLED:
        await redis_client.ping()
        _tasks.extend([asyncio.create_task(publisher()), asyncio.create_task(subscriber())])

async def stop_realtime():
    for task in _tasks: task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    await redis_client.aclose()
