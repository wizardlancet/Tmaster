"""Dashboard-facing REST API: login, enrollment token management, workspace list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.auth import decode_token, issue_access_token, issue_refresh_token, require_user


class LoginBody(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    access_expires_at: int
    refresh_token: str
    refresh_expires_at: int
    user_id: str


class RefreshBody(BaseModel):
    refresh_token: str


class EnrollmentTokenResponse(BaseModel):
    token: str
    expires_in: int


class EnrollBody(BaseModel):
    enrollment_token: str
    agent_name: str


class EnrollResponse(BaseModel):
    agent_id: str
    agent_token: str


def build_router(settings, store, hub) -> APIRouter:
    router = APIRouter(prefix="/api")
    user_dep = require_user(settings)

    @router.post("/auth/login", response_model=TokenResponse)
    async def login(body: LoginBody) -> TokenResponse:
        uid = await store.verify_user(body.username, body.password)
        if uid is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
        access, aexp = issue_access_token(settings, uid)
        refresh, rexp = issue_refresh_token(settings, uid)
        return TokenResponse(
            access_token=access,
            access_expires_at=aexp,
            refresh_token=refresh,
            refresh_expires_at=rexp,
            user_id=uid,
        )

    @router.post("/auth/refresh", response_model=TokenResponse)
    async def refresh(body: RefreshBody) -> TokenResponse:
        uid = decode_token(settings, body.refresh_token, expected_type="refresh")
        access, aexp = issue_access_token(settings, uid)
        newref, rexp = issue_refresh_token(settings, uid)
        return TokenResponse(
            access_token=access,
            access_expires_at=aexp,
            refresh_token=newref,
            refresh_expires_at=rexp,
            user_id=uid,
        )

    @router.post("/agents/enrollment-token", response_model=EnrollmentTokenResponse)
    async def create_enrollment(_: str = Depends(user_dep)) -> EnrollmentTokenResponse:
        tok = await store.create_enrollment_token(ttl_seconds=3600)
        return EnrollmentTokenResponse(token=tok, expires_in=3600)

    @router.post("/agents/enroll", response_model=EnrollResponse)
    async def enroll(body: EnrollBody) -> EnrollResponse:
        res = await store.consume_enrollment_token(
            body.enrollment_token, agent_name=body.agent_name
        )
        if res is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid enrollment token")
        agent_id, agent_token = res
        return EnrollResponse(agent_id=agent_id, agent_token=agent_token)

    @router.get("/workspaces")
    async def list_workspaces(_: str = Depends(user_dep)) -> dict:
        return {"workspaces": await hub._snapshot_workspaces()}

    @router.get("/agents")
    async def list_agents(_: str = Depends(user_dep)) -> dict:
        return {"agents": await store.list_agents()}

    @router.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return router
