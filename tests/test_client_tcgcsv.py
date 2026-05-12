"""Tests for clients.tcgcsv."""

from __future__ import annotations

import httpx
import pytest
import respx

from card_catalog.clients.tcgcsv import BASE_URL, TCGCSV, TCGCSVError


@pytest.fixture
def client():
    c = TCGCSV(user_agent="test-agent/1.0")
    yield c
    c.close()


@respx.mock
def test_list_groups_wrapped_results(client):
    payload = {
        "success": True,
        "results": [
            {"groupId": 1, "name": "Alpha", "abbreviation": "LEA", "publishedOn": "1993-08-05"},
            {"groupId": 2, "name": "Beta", "abbreviation": "LEB", "publishedOn": "1993-10-01"},
        ],
    }
    respx.get(f"{BASE_URL}/tcgplayer/1/groups").mock(
        return_value=httpx.Response(200, json=payload)
    )
    groups = client.list_groups()
    assert len(groups) == 2
    assert groups[0]["abbreviation"] == "LEA"


@respx.mock
def test_list_groups_bare_list(client):
    payload = [{"groupId": 5, "name": "X", "abbreviation": "X"}]
    respx.get(f"{BASE_URL}/tcgplayer/1/groups").mock(
        return_value=httpx.Response(200, json=payload)
    )
    groups = client.list_groups()
    assert groups == payload


@respx.mock
def test_get_prices_returns_rows(client, tcgcsv_prices):
    respx.get(f"{BASE_URL}/tcgplayer/1/42/prices").mock(
        return_value=httpx.Response(200, json=tcgcsv_prices)
    )
    rows = client.get_prices(42)
    assert len(rows) == 3
    assert rows[0]["productId"] == 12345


@respx.mock
def test_last_updated_strips_whitespace(client):
    respx.get(f"{BASE_URL}/last-updated.txt").mock(
        return_value=httpx.Response(200, text="  2026-05-12T20:01:00Z\n")
    )
    assert client.last_updated() == "2026-05-12T20:01:00Z"


@respx.mock
def test_non_200_raises_tcgcsv_error(client):
    respx.get(f"{BASE_URL}/tcgplayer/1/groups").mock(
        return_value=httpx.Response(503, text="busy")
    )
    with pytest.raises(TCGCSVError) as info:
        client.list_groups()
    assert "503" in str(info.value)


@respx.mock
def test_transport_failure_raises_tcgcsv_error(client):
    respx.get(f"{BASE_URL}/tcgplayer/1/groups").mock(
        side_effect=httpx.ConnectError("network down")
    )
    with pytest.raises(TCGCSVError):
        client.list_groups()


@respx.mock
def test_get_prices_rejects_non_list(client):
    respx.get(f"{BASE_URL}/tcgplayer/1/1/prices").mock(
        return_value=httpx.Response(200, json={"success": True, "data": "wrong"})
    )
    with pytest.raises(TCGCSVError):
        client.get_prices(1)


@respx.mock
def test_user_agent_header_is_set():
    c = TCGCSV(user_agent="trace-me/1.2")
    route = respx.get(f"{BASE_URL}/last-updated.txt").mock(
        return_value=httpx.Response(200, text="2026-01-01T00:00:00Z")
    )
    c.last_updated()
    c.close()
    assert route.calls.last.request.headers["user-agent"] == "trace-me/1.2"
