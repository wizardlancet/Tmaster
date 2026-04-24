from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def app_tc(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        bootstrap_user="admin",
        bootstrap_password="hunter2",
        jwt_secret="test-secret",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_healthz(app_tc):
    r = app_tc.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_login_and_protected(app_tc):
    r = app_tc.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    r2 = app_tc.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json() == {"workspaces": []}

    r3 = app_tc.get("/api/workspaces")
    assert r3.status_code == 401


def test_login_bad_password(app_tc):
    r = app_tc.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_enrollment_flow(app_tc):
    # Login first
    r = app_tc.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
    token = r.json()["access_token"]

    r = app_tc.post(
        "/api/agents/enrollment-token", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    etoken = r.json()["token"]

    r = app_tc.post(
        "/api/agents/enroll",
        json={"enrollment_token": etoken, "agent_name": "laptop-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] and body["agent_token"]

    # Second use must fail
    r = app_tc.post(
        "/api/agents/enroll",
        json={"enrollment_token": etoken, "agent_name": "laptop-2"},
    )
    assert r.status_code == 401
