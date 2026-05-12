"""Router tests: /settings."""

from __future__ import annotations


def test_settings_page_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    # The form id is hard-coded in the partial.
    assert 'id="settings-form"' in r.text


def test_settings_save_persists_value(client):
    r = client.post(
        "/settings",
        data={"display_currency": "EUR", "page_size": "120"},
    )
    assert r.status_code == 200
    # Fragment should include the "Saved" toast.
    assert "Saved" in r.text

    # Re-render the page; verify persistence.
    r2 = client.get("/settings")
    assert r2.status_code == 200
    # The select should show EUR as selected; check the option attribute appears.
    assert "EUR" in r2.text


def test_settings_save_ignores_unknown_keys(client):
    r = client.post(
        "/settings",
        data={"random_bogus_key": "x", "display_currency": "USD"},
    )
    assert r.status_code == 200
    # No crash, response includes the form again.
    assert 'id="settings-form"' in r.text
