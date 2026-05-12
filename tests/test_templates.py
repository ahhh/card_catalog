"""Template smoke tests.

Two layers:
1. Every .html template under templates/ parses without a Jinja syntax error.
2. The full pages render via the router test client for typical contexts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError

from card_catalog.config import settings


TEMPLATES_DIR = Path(settings.templates_dir)


def _all_templates() -> list[Path]:
    return [p for p in TEMPLATES_DIR.rglob("*.html")]


@pytest.mark.parametrize("template_path", _all_templates(), ids=lambda p: p.name)
def test_template_parses(template_path):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    rel = template_path.relative_to(TEMPLATES_DIR).as_posix()
    try:
        env.get_template(rel)
    except TemplateSyntaxError as exc:
        pytest.fail(f"Jinja syntax error in {rel}: {exc.message} (line {exc.lineno})")


def test_dashboard_render_via_client_smoke(client):
    """Full integration: dashboard renders with empty DB."""
    r = client.get("/")
    assert r.status_code == 200
    # base.html was applied (doctype) and nav included.
    assert "<!doctype html>" in r.text.lower() or "<!DOCTYPE html>" in r.text
    assert "Card Catalog" in r.text


def test_collection_render_smoke(client, sample_entry):
    r = client.get("/collection")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text


def test_settings_render_smoke(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text


def test_import_render_smoke(client):
    r = client.get("/import")
    assert r.status_code == 200


def test_prices_render_smoke(client):
    r = client.get("/prices")
    assert r.status_code == 200


def test_archidekt_render_smoke(client):
    r = client.get("/archidekt")
    assert r.status_code == 200


def test_card_detail_render_smoke(client, sample_card):
    r = client.get(f"/cards/{sample_card.scryfall_id}")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text


def test_404_render_smoke(client):
    r = client.get("/__missing__")
    assert r.status_code == 404


def test_macros_module_loads():
    """Macros file should parse and expose the documented macros."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    mod = env.get_template("partials/macros.html").module
    for macro in ("mana_cost", "rarity_pip", "color_dots", "money", "card_tile"):
        assert hasattr(mod, macro), f"macro {macro} missing from macros.html"
