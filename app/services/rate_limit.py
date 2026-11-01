"""Separate origin creation, origin updates, and authenticated relay budgets.

Call only for an accepted new ID/revision, under the SOS advisory lock. Duplicate
uploads do not consume the budget needed by a busy relay to carry other people.
"""
import math
import time
from fastapi import HTTPException
from app.config import settings

_windows: dict[str, tuple[float, int]] = {}


async def check_submission(actor: str, owner: str, *, new: bool, relayed: bool):
    budgets = []
    if new:
        budgets.append((f"origin:{owner}", settings.SOS_RATE_LIMIT_PER_MINUTE))
    elif not relayed:
        budgets.append((f"update:{owner}", settings.SOS_UPDATE_RATE_LIMIT_PER_MINUTE))
    if relayed:
        budgets.append((f"relay:{actor}", settings.SOS_RELAY_RATE_LIMIT_PER_MINUTE))
    if settings.REDIS_ENABLED:
        from app.realtime import redis_client
        # Check every budget before incrementing any of them. Concurrent workers
        # cannot partially charge a rejected submission or bypass a limit.
        script = """
        for i,key in ipairs(KEYS) do
          if tonumber(redis.call('GET',key) or '0') >= tonumber(ARGV[i]) then
            return math.max(1,redis.call('TTL',key))
          end
        end
        for _,key in ipairs(KEYS) do
          if redis.call('INCR',key)==1 then redis.call('EXPIRE',key,60) end
        end
        return 0
        """
        try:
            retry = await redis_client.eval(script, len(budgets),
                *(f"sos:budget:{key}" for key, _ in budgets), *(limit for _, limit in budgets))
        except Exception:
            raise HTTPException(503, "Rate limiter unavailable", headers={"Retry-After": "10"}) from None
    else:
        now = time.monotonic()
        for key, (until, _) in list(_windows.items()):
            if until <= now:
                del _windows[key]
        retry = max((math.ceil(_windows[key][0] - now) for key, limit in budgets
            if key in _windows and _windows[key][1] >= limit), default=0)
        if not retry:
            for key, _ in budgets:
                until, count = _windows.get(key, (now + 60, 0))
                _windows[key] = (until, count + 1)
    if retry:
        raise HTTPException(429, "Submission limit reached; this SOS remains queued",
                            headers={"Retry-After": str(retry)})
