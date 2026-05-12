"""Tests for services.settings."""

from __future__ import annotations

from card_catalog.db.models import Setting
from card_catalog.services import settings as svc


def test_ensure_defaults_seeds_all_keys(db):
    svc.ensure_defaults(db)
    rows = {s.key: s.value for s in db.query(Setting).all()}
    for key, default in svc.SETTING_DEFAULTS.items():
        assert rows[key] == default


def test_ensure_defaults_is_idempotent(db):
    svc.ensure_defaults(db)
    # Mutate one value, then re-ensure: shouldn't reset it.
    row = db.get(Setting, "display_currency")
    row.value = "EUR"
    db.commit()
    svc.ensure_defaults(db)
    assert db.get(Setting, "display_currency").value == "EUR"


def test_get_all_fills_in_defaults_for_missing_keys(db):
    # No ensure_defaults called yet.
    values = svc.get_all(db)
    for key, default in svc.SETTING_DEFAULTS.items():
        assert values[key] == default


def test_get_all_merges_db_values(db):
    svc.ensure_defaults(db)
    db.get(Setting, "page_size").value = "120"
    db.commit()
    assert svc.get_all(db)["page_size"] == "120"


def test_get_returns_default_when_missing(db):
    assert svc.get(db, "default_condition") == "NM"
    assert svc.get(db, "unknown_thing", default="fallback") == "fallback"


def test_set_many_upserts(db):
    svc.ensure_defaults(db)
    svc.set_many(db, {"page_size": "240", "display_currency": "GBP"})
    assert svc.get(db, "page_size") == "240"
    assert svc.get(db, "display_currency") == "GBP"


def test_set_many_inserts_new_keys(db):
    svc.set_many(db, {"brand_new_key": "x"})
    assert db.get(Setting, "brand_new_key").value == "x"


def test_setting_specs_are_well_formed():
    keys = {s.key for s in svc.SETTING_SPECS}
    # All specs reference a known default.
    assert keys.issubset(set(svc.SETTING_DEFAULTS.keys()))
    for spec in svc.SETTING_SPECS:
        assert spec.label
        assert spec.kind in {"text", "select", "secret", "number"}
        if spec.kind == "select":
            assert spec.options and len(spec.options) >= 1
