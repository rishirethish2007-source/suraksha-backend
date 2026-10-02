"""Authentication is supplied by the deployment's identity provider."""
from fastapi import Header, HTTPException
from jose import JWTError, jwt
from app.config import settings


def validate_token(authorization: str | None = Header(None)) -> dict:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid or missing authentication token")
    if len(settings.JWT_SECRET_KEY) < 32:
        raise HTTPException(503, "Authentication is not configured")
    try:
        claims = jwt.decode(
            authorization[7:], settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require_exp": True, "require_sub": True},
        )
        if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
            raise JWTError("Missing subject")
        return claims
    except JWTError:
        raise HTTPException(401, "Invalid or expired authentication token") from None


def require_subject(claims: dict, user_id: str):
    if claims["sub"] != user_id:
        raise HTTPException(403, "User identity does not match the authenticated user")


def require_responder(claims: dict):
    if claims.get("role") not in {"responder", "admin"}:
        raise HTTPException(403, "Responder access required")
