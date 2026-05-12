"""Scryfall HTTP client.

Sync httpx client with per-endpoint token-bucket rate limiting.
Scryfall rate-limit rules (https://scryfall.com/docs/api):
  - `/cards/search`, `/cards/named`, `/cards/random`, `/cards/collection` → 500ms
  - everything else → 100ms (or higher if user configured a larger delay).

Sharing a single bucket across endpoint classes triggers 429s under the stricter
search bucket, so we keep one bucket per path-class.

This module owns HTTP + rate limiting. It does *not* touch the DB; callers map
dicts into ORM rows.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import ijson

from card_catalog.config import settings

log = logging.getLogger(__name__)

SCRYFALL_BASE_URL = "https://api.scryfall.com"

_STRICT_PREFIXES = (
    "/cards/search",
    "/cards/named",
    "/cards/random",
    "/cards/collection",
)
_STRICT_DELAY_MS = 500
_DEFAULT_DELAY_MS = 100


class _TokenBucket:
    """A trivial per-endpoint sleep-until-next-allowed bucket."""

    def __init__(self, min_interval_ms: int) -> None:
        self._lock = threading.Lock()
        self._interval = max(min_interval_ms, 0) / 1000.0
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                delay = self._next_at - now
            else:
                delay = 0.0
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            time.sleep(delay)


class ScryfallClient:
    """Stateful client. One per process is plenty; share across threads safely."""

    def __init__(
        self,
        user_agent: str | None = None,
        default_delay_ms: int | None = None,
        base_url: str = SCRYFALL_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        ua = user_agent or settings.scryfall_user_agent
        default_delay = max(
            default_delay_ms if default_delay_ms is not None else settings.scryfall_delay_ms,
            _DEFAULT_DELAY_MS,
        )
        self._default_bucket = _TokenBucket(default_delay)
        self._strict_bucket = _TokenBucket(max(_STRICT_DELAY_MS, default_delay))
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "User-Agent": ua,
                "Accept": "application/json;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        )

    # ---- internal helpers --------------------------------------------------

    def _bucket_for(self, path: str) -> _TokenBucket:
        for prefix in _STRICT_PREFIXES:
            if path.startswith(prefix):
                return self._strict_bucket
        return self._default_bucket

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._bucket_for(path).wait()
        resp = self._client.request(method, path, **kwargs)
        return resp

    def _get_json(self, path: str, *, allow_404: bool = False, **kwargs) -> dict | None:
        resp = self._request("GET", path, **kwargs)
        if allow_404 and resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ---- card lookups ------------------------------------------------------

    def get_by_set_number(
        self, set_code: str, collector_number: str, lang: str = "en"
    ) -> dict | None:
        """GET /cards/{code}/{number}[/{lang}]. Returns Scryfall raw dict or None on 404."""
        code = (set_code or "").strip().lower()
        number = str(collector_number or "").strip()
        if not code or not number:
            return None
        path = f"/cards/{code}/{number}"
        if lang and lang != "en":
            path = f"{path}/{lang}"
        try:
            return self._get_json(path, allow_404=True)
        except httpx.HTTPStatusError as exc:
            log.warning("scryfall %s failed: %s", path, exc)
            return None

    def get_collection(self, identifiers: list[dict]) -> dict:
        """POST /cards/collection in batches of ≤75; merged response.

        identifiers: a list of identifier dicts as documented by Scryfall, e.g.:
          {"id": "<scryfall-id>"}
          {"set": "neo", "collector_number": "123"}
        """
        found: list[dict] = []
        not_found: list[dict] = []
        for i in range(0, len(identifiers), 75):
            batch = identifiers[i : i + 75]
            resp = self._request(
                "POST",
                "/cards/collection",
                json={"identifiers": batch},
            )
            if resp.status_code >= 400:
                # Scryfall returns 422 for malformed payloads; treat batch as all-not-found
                # rather than blowing up the entire import preview.
                log.warning(
                    "scryfall /cards/collection returned %s (batch %d-%d)",
                    resp.status_code,
                    i,
                    i + len(batch),
                )
                not_found.extend(batch)
                continue
            data = resp.json()
            found.extend(data.get("data", []) or [])
            not_found.extend(data.get("not_found", []) or [])
        return {"found": found, "not_found": not_found}

    # ---- bulk data ---------------------------------------------------------

    def get_bulk_data_index(self) -> list[dict]:
        """GET /bulk-data. Returns the `data` array of bulk-file descriptors."""
        payload = self._get_json("/bulk-data") or {}
        return list(payload.get("data") or [])

    def find_bulk_descriptor(self, bulk_type: str = "default_cards") -> dict | None:
        for entry in self.get_bulk_data_index():
            if entry.get("type") == bulk_type:
                return entry
        return None

    def stream_bulk(
        self,
        url: str,
        on_card: Callable[[dict], None],
        *,
        download_dir: Path | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Download bulk file to data/bulk/ then stream-parse via ijson.

        Calls `on_card(card_dict)` for every entry. Returns the total count parsed.
        `progress(downloaded_bytes, total_bytes)` is called periodically during download.
        """
        target_dir = download_dir or (settings.data_dir / "bulk")
        target_dir.mkdir(parents=True, exist_ok=True)
        # Filename from the URL tail; safe because Scryfall URLs are predictable.
        filename = url.rsplit("/", 1)[-1] or "bulk.json"
        local_path = target_dir / filename

        # Download via streamed GET so we don't load the whole file into memory.
        # Bulk downloads use the c2.scryfall.com CDN — not rate-limited the same way,
        # but we respect the default bucket once on the way in.
        self._default_bucket.wait()
        with httpx.stream(
            "GET",
            url,
            timeout=300.0,
            headers={
                "User-Agent": self._client.headers.get("User-Agent", ""),
                "Accept": "application/json;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(local_path, "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)

        count = 0
        with open(local_path, "rb") as fh:
            for card in ijson.items(fh, "item"):
                on_card(card)
                count += 1
        return count

    # ---- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ScryfallClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# Module-level default client. Most callers reuse this; tests instantiate fresh ones.
_default_client: ScryfallClient | None = None
_default_lock = threading.Lock()


def get_default_client() -> ScryfallClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = ScryfallClient()
        return _default_client


def reset_default_client() -> None:
    """Drop the cached client (e.g., after settings change)."""
    global _default_client
    with _default_lock:
        if _default_client is not None:
            try:
                _default_client.close()
            except Exception:  # noqa: BLE001
                pass
        _default_client = None


__all__ = [
    "ScryfallClient",
    "get_default_client",
    "reset_default_client",
    "SCRYFALL_BASE_URL",
]
