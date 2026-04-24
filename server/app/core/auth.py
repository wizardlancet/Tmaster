"""JWT-based auth for the dashboard, plus agent token helpers."""

from __future__ import annotations

import time
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Query, WebSocket, status

from app.core.config import Settings
from app.core.store import Store


def _now() -> int:
    return int(time.time())


def issue_access_token(settings: Settings, user_id: str) -> tuple[str, int]:
    exp = _now() + settings.jwt_access_ttl_seconds
    token = jwt.encode(
        {"sub": user_id, "exp": exp, "typ": "access"},
        settings.resolve_jwt_secret(),
        algorithm="HS256",
    )
    return token, exp


def issue_refresh_token(settings: Settings, user_id: str) -> tuple[str, int]:
    exp = _now() + settings.jwt_refresh_ttl_seconds
    token = jwt.encode(
        {"sub": user_id, "exp": exp, "typ": "refresh"},
        settings.resolve_jwt_secret(),
        algorithm="HS256",
    )
    return token, exp


def decode_token(settings: Settings, token: str, *, expected_type: str) -> str:
    try:
        claims = jwt.decode(token, settings.resolve_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {e}") from e
    if claims.get("typ") != expected_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong token type")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing subject")
    return str(sub)


# ---- FastAPI dependencies ------------------------------------------------


def require_user(settings: Settings):
    def _dep(authorization: Optional[str] = Header(default=None)) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        token = authorization.split(" ", 1)[1]
        return decode_token(settings, token, expected_type="access")

    return _dep


async def authenticate_websocket_dashboard(
    websocket: WebSocket,
    settings: Settings,
    *,
    token: Optional[str],
) -> Optional[str]:
    """Return user_id or None. Does not close the websocket on failure — caller decides."""
    if token is None:
        return None
    try:
        return decode_token(settings, token, expected_type="access")
    except HTTPException:
        return None


async def authenticate_agent(
    store: Store, *, agent_id: str, token: str
) -> bool:
    return await store.verify_agent_token(agent_id, token)


# ---- Query-param extractor for WS tokens --------------------------------


def ws_token(token: Optional[str] = Query(default=None)) -> Optional[str]:
    return token
