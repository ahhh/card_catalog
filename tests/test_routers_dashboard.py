"""Router test: dashboard ('/')."""

from __future__ import annotations


def test_dashboard_renders_when_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    # Some signature strings from the template.
    assert "Welcome back" in r.text
    assert "The Vault" in r.text or "Vault" in r.text


def test_dashboard_with_data(client, sample_entry):
    r = client.get("/")
    assert r.status_code == 200
    # quantity 2 should be reflected in totals somewhere.
    assert "Lightning Bolt" in r.text or "Total cards" in r.text


def test_dashboard_navlink_active(client):
    r = client.get("/")
    assert 'aria-current="page"' in r.text


def test_404_uses_custom_handler(client):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert "isn't in the catalog" in r.text or "Not found" in r.text
