"""FastAPI routes — TestClient smoke tests (isolated seeded DB)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(seeded_sqlite_db):
    """HTTP client against ``api.routes.app`` using the test SQLite URL."""
    from api.routes import app

    return TestClient(app)


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["database"] is True
    assert "version" in body


def test_stock_summary_json(client):
    from config.settings import settings

    r = client.get(f"/api/facilities/{settings.simulation_facility_id}/stock")
    assert r.status_code == 200
    data = r.json()
    assert data["facility_id"] == settings.simulation_facility_id
    assert data["include_batches"] is False
    assert "drugs" in data
    assert isinstance(data["drugs"], list)
    assert len(data["drugs"]) >= 1
    first = data["drugs"][0]
    assert "drug_id" in first
    assert "drug_name" in first
    assert "total_quantity" in first


def test_audit_log_structure(client):
    from config.settings import settings

    r = client.get(
        f"/api/facilities/{settings.simulation_facility_id}/audit-log",
        params={"page": 1, "page_size": 5},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 0
    assert "items" in data
    assert isinstance(data["items"], list)


def test_drugs_catalog(client):
    r = client.get("/api/drugs", params={"limit": 10, "offset": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1
    assert "name" in data["items"][0]


def test_websocket_connect_and_disconnect(client):
    with client.websocket_connect("/ws/events") as ws:
        # Default: subscribed to agent + orchestrator topics; no events until bus publishes.
        pass
