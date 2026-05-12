"""Shared pytest fixtures.

Every test gets a fresh in-memory-ish SQLite database. We monkeypatch
`card_catalog.db.session.engine` and `SessionLocal` so any module that
imported them directly (routers/prices.py opens its own session in a
worker thread) sees the per-test database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from card_catalog.db import session as session_module
from card_catalog.db.models import Base


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---- DB isolation ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Replace the module-level engine + sessionmaker with a per-test SQLite."""
    db_path = tmp_path / "test.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    SessionTest = sessionmaker(
        bind=eng, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(session_module, "engine", eng)
    monkeypatch.setattr(session_module, "SessionLocal", SessionTest)
    # Routers/services that imported `SessionLocal` by name capture the
    # original binding. Re-patch each one so their thread-local DB sessions
    # also see the per-test engine.
    for mod_path in (
        "card_catalog.main",
        "card_catalog.routers.imports",
        "card_catalog.routers.prices",
    ):
        import importlib

        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", SessionTest)
    yield
    eng.dispose()


# ---- DB session for direct tests ------------------------------------------


@pytest.fixture
def db():
    s = session_module.SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---- Job registry isolation (avoid bleed between tests) -------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    from card_catalog.jobs.registry import registry as _reg

    _reg._jobs.clear()
    yield
    _reg._jobs.clear()


# ---- Preview store isolation (import_manabox) -----------------------------


@pytest.fixture(autouse=True)
def _clean_preview_store():
    from card_catalog.services import import_manabox

    import_manabox._preview_store.clear()
    yield
    import_manabox._preview_store.clear()


# ---- HTTP test client ------------------------------------------------------


@pytest.fixture
def client():
    """FastAPI TestClient with `get_db` pointed at our per-test session."""
    from card_catalog.main import app
    from card_catalog.db.session import get_db

    def _get_db():
        s = session_module.SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- Sample model data -----------------------------------------------------


@pytest.fixture
def sample_card(db):
    """A ScryfallCard with realistic fields. Returns the model instance, committed."""
    from card_catalog.db.models import ScryfallCard

    card = ScryfallCard(
        scryfall_id="00000000-0000-0000-0000-000000000001",
        oracle_id="oracle-001",
        name="Lightning Bolt",
        set_code="lea",
        set_name="Limited Edition Alpha",
        collector_number="161",
        rarity="common",
        lang="en",
        type_line="Instant",
        oracle_text="Lightning Bolt deals 3 damage to any target.",
        mana_cost="{R}",
        cmc=1.0,
        colors=json.dumps(["R"]),
        color_identity=json.dumps(["R"]),
        finishes=json.dumps(["nonfoil"]),
        image_normal_uri="https://example.com/lb.jpg",
        image_small_uri="https://example.com/lb_small.jpg",
        tcgplayer_id=12345,
        raw_json="{}",
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


@pytest.fixture
def sample_entry(db, sample_card):
    """A CollectionEntry pointing at sample_card."""
    from card_catalog.db.models import CollectionEntry

    e = CollectionEntry(
        scryfall_id=sample_card.scryfall_id,
        finish="nonfoil",
        condition="NM",
        language="en",
        quantity=2,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# ---- Sample JSON fixtures (lazily loaded) ---------------------------------


@pytest.fixture
def scryfall_card_dict():
    with open(FIXTURES_DIR / "scryfall_card.json") as fh:
        return json.load(fh)


@pytest.fixture
def scryfall_bulk_index():
    with open(FIXTURES_DIR / "scryfall_bulk_data.json") as fh:
        return json.load(fh)


@pytest.fixture
def tcgcsv_prices():
    with open(FIXTURES_DIR / "tcgcsv_prices.json") as fh:
        return json.load(fh)
