"""Tests for clients.scryfall (rate limiter + endpoint shapes via respx)."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from card_catalog.clients.scryfall import (
    SCRYFALL_BASE_URL,
    ScryfallClient,
    _TokenBucket,
)


@pytest.fixture
def fast_client():
    """A ScryfallClient with the strict bucket lowered to 0 for general tests."""
    cl = ScryfallClient(user_agent="card_catalog-test/0.0 (test)", default_delay_ms=0)
    # Force buckets to zero-delay; rate-limit assertions use real buckets explicitly.
    cl._default_bucket = _TokenBucket(0)
    cl._strict_bucket = _TokenBucket(0)
    yield cl
    cl.close()


@respx.mock
def test_get_by_set_number_returns_card(fast_client, scryfall_card_dict):
    route = respx.get(f"{SCRYFALL_BASE_URL}/cards/lea/161").mock(
        return_value=httpx.Response(200, json=scryfall_card_dict)
    )
    out = fast_client.get_by_set_number("LEA", "161")
    assert route.called
    assert out["name"] == "Lightning Bolt"


@respx.mock
def test_get_by_set_number_with_lang_uses_localized_url(
    fast_client, scryfall_card_dict
):
    route = respx.get(f"{SCRYFALL_BASE_URL}/cards/neo/123/ja").mock(
        return_value=httpx.Response(200, json=scryfall_card_dict)
    )
    fast_client.get_by_set_number("neo", "123", lang="ja")
    assert route.called


@respx.mock
def test_get_by_set_number_404_returns_none(fast_client):
    respx.get(f"{SCRYFALL_BASE_URL}/cards/zzz/999").mock(
        return_value=httpx.Response(404, json={"object": "error"})
    )
    assert fast_client.get_by_set_number("zzz", "999") is None


@respx.mock
def test_get_by_set_number_empty_inputs():
    cl = ScryfallClient(default_delay_ms=0)
    cl._default_bucket = _TokenBucket(0)
    assert cl.get_by_set_number("", "1") is None
    assert cl.get_by_set_number("lea", "") is None
    cl.close()


@respx.mock
def test_get_collection_batches_over_75(fast_client, scryfall_card_dict):
    """76 identifiers should yield two POSTs and merged found/not_found."""
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        payload = json.loads(request.content.decode())
        ids = payload["identifiers"]
        # Echo each identifier back as a "found" card stub.
        data = []
        for ident in ids:
            d = dict(scryfall_card_dict)
            d["id"] = f"id-{calls['n']}-{ident.get('id', ident.get('collector_number', '?'))}"
            data.append(d)
        return httpx.Response(200, json={"data": data, "not_found": []})

    respx.post(f"{SCRYFALL_BASE_URL}/cards/collection").mock(side_effect=_handler)
    idents = [{"id": f"sid-{i}"} for i in range(76)]
    result = fast_client.get_collection(idents)
    assert calls["n"] == 2
    assert len(result["found"]) == 76
    assert result["not_found"] == []


@respx.mock
def test_get_collection_treats_4xx_batch_as_not_found(fast_client):
    respx.post(f"{SCRYFALL_BASE_URL}/cards/collection").mock(
        return_value=httpx.Response(422, json={"object": "error"})
    )
    out = fast_client.get_collection([{"id": "x"}, {"id": "y"}])
    assert out["found"] == []
    assert len(out["not_found"]) == 2


@respx.mock
def test_get_bulk_data_index_returns_list(fast_client, scryfall_bulk_index):
    respx.get(f"{SCRYFALL_BASE_URL}/bulk-data").mock(
        return_value=httpx.Response(200, json=scryfall_bulk_index)
    )
    entries = fast_client.get_bulk_data_index()
    assert len(entries) == 2
    assert entries[0]["type"] == "default_cards"


@respx.mock
def test_find_bulk_descriptor_picks_by_type(fast_client, scryfall_bulk_index):
    respx.get(f"{SCRYFALL_BASE_URL}/bulk-data").mock(
        return_value=httpx.Response(200, json=scryfall_bulk_index)
    )
    desc = fast_client.find_bulk_descriptor("all_cards")
    assert desc["type"] == "all_cards"
    assert fast_client.find_bulk_descriptor("missing-kind") is None


@respx.mock
def test_user_agent_header_is_set(scryfall_card_dict):
    cl = ScryfallClient(user_agent="custom-agent/9.9", default_delay_ms=0)
    cl._default_bucket = _TokenBucket(0)
    cl._strict_bucket = _TokenBucket(0)
    route = respx.get(f"{SCRYFALL_BASE_URL}/cards/lea/161").mock(
        return_value=httpx.Response(200, json=scryfall_card_dict)
    )
    cl.get_by_set_number("lea", "161")
    cl.close()
    assert route.calls.last.request.headers["user-agent"] == "custom-agent/9.9"


# ---- rate limiter timing ---------------------------------------------------


def test_token_bucket_enforces_min_interval():
    """Two consecutive wait() calls on a 100ms bucket must take ≥ 100ms total."""
    b = _TokenBucket(100)
    t0 = time.monotonic()
    b.wait()  # primes _next_at
    b.wait()  # this one must sleep ~100ms
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09  # generous lower bound for CI jitter


def test_strict_vs_default_bucket_independence():
    """Different path classes don't share a bucket."""
    cl = ScryfallClient(default_delay_ms=0)
    assert cl._bucket_for("/cards/collection") is cl._strict_bucket
    assert cl._bucket_for("/cards/search") is cl._strict_bucket
    assert cl._bucket_for("/bulk-data") is cl._default_bucket
    assert cl._bucket_for("/cards/lea/161") is cl._default_bucket
    cl.close()
