"""Responder dashboard. The HTML shell is public; incident data requires a role."""
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth import validate_token, require_responder
from app.db.database import get_db
from app.models.sos import SOSEvent
from app.services.sos_service import _convert_to_response

router = APIRouter()
ASSETS = Path(__file__).resolve().parents[1] / "dashboard"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-src https://www.openstreetmap.org; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}

@router.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(ASSETS / "index.html", headers=HEADERS)

@router.get("/dashboard/app.js", include_in_schema=False)
async def dashboard_js():
    return FileResponse(ASSETS / "app.js", media_type="text/javascript", headers=HEADERS)

@router.get("/dashboard/style.css", include_in_schema=False)
async def dashboard_css():
    return FileResponse(ASSETS / "style.css", media_type="text/css", headers=HEADERS)

@router.get("/api/v1/dashboard/events", tags=["Responder dashboard"])
async def dashboard_events(
    response: Response,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(validate_token),
):
    require_responder(claims)
    response.headers["Cache-Control"] = "no-store"
    stmt = select(SOSEvent).where(SOSEvent.status.in_(["ACTIVE", "ACKNOWLEDGED", "RESPONDING"]))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.where(func.extract("epoch", now - SOSEvent.client_timestamp) < SOSEvent.ttl_seconds)
    else:  # SQLite is used only by unit tests.
        stmt = stmt.where((func.julianday(now) - func.julianday(SOSEvent.client_timestamp)) * 86400 < SOSEvent.ttl_seconds)
    rows = (await db.execute(stmt.order_by(SOSEvent.created_at.desc(), SOSEvent.id.desc()).offset(offset).limit(limit + 1))).scalars().all()
    return {"events": [_convert_to_response(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
            "server_time": datetime.now(timezone.utc).isoformat()}
