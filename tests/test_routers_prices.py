"""Router tests: /prices/*."""

from __future__ import annotations


def test_prices_page_empty(client):
    r = client.get("/prices")
    assert r.status_code == 200
    assert "Prices" in r.text


def test_prices_page_with_collection(client, sample_entry):
    r = client.get("/prices")
    assert r.status_code == 200


def test_refresh_rejected_when_collection_empty(client):
    r = client.post("/prices/refresh")
    assert r.status_code == 400


def test_job_status_unknown_returns_404(client):
    r = client.get("/prices/jobs/no-such-id")
    assert r.status_code == 404


def test_job_status_terminal_emits_toast(client, sample_entry):
    """Manually inject a job into the registry, then poll its status."""
    from card_catalog.jobs.registry import JobStatus, registry

    j = registry.create("prices_refresh", "manual", total=1)
    j.done = 1
    j.status = JobStatus.COMPLETED
    j.extras["updates"] = 17

    r = client.get(f"/prices/jobs/{j.id}")
    assert r.status_code == 200
    # Terminal jobs emit a one-shot HX-Toast header.
    assert "HX-Toast" in r.headers
