"""Pytest configuration — project root on path via pyproject ``pythonpath``."""

from __future__ import annotations

import pytest

from config.settings import settings


@pytest.fixture
def sqlite_tmp_db(monkeypatch, tmp_path):
    """
    Isolated SQLite file + fresh schema. Does not seed rows unless the test does.
    """
    url = f"sqlite:///{tmp_path / 'test_estock.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    from core.database import init_db, rebind_engine

    rebind_engine(url)
    init_db()
    return url


@pytest.fixture
def seeded_sqlite_db(monkeypatch, tmp_path):
    """Fresh DB + full drug/stock seed (same JSON as ``scripts/seed_db.py``)."""
    url = f"sqlite:///{tmp_path / 'seeded_estock.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    from core.database import get_db, init_db, rebind_engine
    from scripts.seed_db import _load_json, seed_drugs, seed_stock

    rebind_engine(url)
    init_db()
    with get_db() as session:
        drug_map = seed_drugs(session, _load_json(settings.drugs_catalog_path))
        seed_stock(session, _load_json(settings.seed_stock_path), drug_map)
    return url
