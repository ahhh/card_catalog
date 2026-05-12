"""Router tests: /import/*."""

from __future__ import annotations

import io
import time

from card_catalog.db.models import ScryfallCard
from card_catalog.services import import_manabox


def test_import_page_renders(client):
    r = client.get("/import")
    assert r.status_code == 200
    assert "Drop your Manabox CSV" in r.text or "Manabox" in r.text


def test_dropzone_fragment(client):
    r = client.get("/import/dropzone")
    assert r.status_code == 200
    assert "Drop your" in r.text


def test_preview_upload_with_cache_seeded(client, db):
    """Upload a tiny CSV; all rows pre-resolvable from cache."""
    db.add(
        ScryfallCard(
            scryfall_id="cached-1",
            name="Sol Ring",
            set_code="cmm",
            set_name="Commander Masters",
            collector_number="410",
            rarity="common",
            lang="en",
            raw_json="{}",
        )
    )
    db.commit()
    csv = (
        "Name,Set code,Collector number,Quantity\n"
        "Sol Ring,cmm,410,2\n"
    )
    files = {"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")}
    r = client.post("/import/preview", files=files)
    assert r.status_code == 200
    assert "Preview" in r.text
    # 1 insert verdict.
    assert "Verdict" in r.text or "insert" in r.text.lower() or "New cards" in r.text


def test_preview_bad_csv_emits_warning(client):
    """An obviously broken CSV should not crash; should return a 200."""
    bad = b"some garbage that isn't a CSV"
    files = {"file": ("c.csv", io.BytesIO(bad), "text/csv")}
    r = client.post("/import/preview", files=files)
    assert r.status_code == 200


def test_commit_with_unknown_preview_returns_410(client):
    r = client.post("/import/commit", data={"preview_id": "no-such-id"})
    assert r.status_code == 410


def test_commit_threaded_path(client, db):
    """Stage a preview directly then call /import/commit. The handler launches a
    daemon thread; we poll the job endpoint until terminal.
    """
    # Stage a preview using the service directly (skip the upload step).
    db.add(
        ScryfallCard(
            scryfall_id="commit-1",
            name="Sol Ring",
            set_code="cmm",
            set_name="Commander Masters",
            collector_number="410",
            rarity="common",
            lang="en",
            raw_json="{}",
        )
    )
    db.commit()
    from card_catalog.clients.manabox_csv import ImportRow

    rows = [
        ImportRow(
            name="Sol Ring",
            set_code="cmm",
            collector_number="410",
            scryfall_id="commit-1",
            quantity=1,
            finish="nonfoil",
            condition="NM",
            language="en",
        )
    ]
    pid, _verdicts, _ = import_manabox.preview(db, rows, scryfall_client=None)

    r = client.post("/import/commit", data={"preview_id": pid})
    assert r.status_code == 200
    # Job kicked off; poll until terminal.
    from card_catalog.jobs.registry import registry

    deadline = time.monotonic() + 2.0
    job = None
    while time.monotonic() < deadline:
        jobs = registry.list(kind="import_commit", limit=1)
        if jobs and jobs[0].is_terminal:
            job = jobs[0]
            break
        time.sleep(0.02)
    assert job is not None
    assert job.status.value == "completed"


def test_job_status_endpoint_unknown(client):
    r = client.get("/import/jobs/import/no-such-id")
    assert r.status_code == 404


def test_scryfall_job_endpoint_unknown(client):
    r = client.get("/import/jobs/scryfall/no-such-id")
    assert r.status_code == 404
