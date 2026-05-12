"""TCGCSV HTTP client.

TCGCSV publishes daily TCGplayer snapshots at https://tcgcsv.com. Magic: The
Gathering lives under categoryId 1. The site asks two things of us:

  1. a custom User-Agent (no default httpx UA),
  2. a polite 100ms gap between requests.

Both are enforced here so callers can't accidentally hammer the service.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from card_catalog.config import settings

MTG_CATEGORY_ID = 1
BASE_URL = "https://tcgcsv.com"
_POLITE_DELAY_S = 0.1


class TCGCSVError(RuntimeError):
    """Friendly error for any non-200 response or transport failure."""


class TCGCSV:
    """Sync TCGCSV client. One instance per refresh run is plenty."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        ua = user_agent or settings.scryfall_user_agent
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "User-Agent": ua,
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.5",
            },
            timeout=timeout,
        )
        self._lock = threading.Lock()
        self._last_request_at: float = 0.0

    # ---- internals ---------------------------------------------------------

    def _request(self, path: str) -> httpx.Response:
        """GET `path` with a per-client 100ms gap and friendly error wrapping."""
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < _POLITE_DELAY_S:
                time.sleep(_POLITE_DELAY_S - elapsed)
            try:
                resp = self._client.get(path)
            except httpx.HTTPError as exc:
                raise TCGCSVError(f"TCGCSV request to {path} failed: {exc}") from exc
            finally:
                self._last_request_at = time.monotonic()

        if resp.status_code != 200:
            raise TCGCSVError(
                f"TCGCSV returned HTTP {resp.status_code} for {path}: "
                f"{resp.text[:200] if resp.text else '(empty body)'}"
            )
        return resp

    def _get_json(self, path: str) -> Any:
        resp = self._request(path)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise TCGCSVError(f"TCGCSV returned non-JSON for {path}: {exc}") from exc
        # TCGCSV wraps lists in {"success": true, "results": [...]} or returns the
        # bare list. Accept both shapes so we don't break on a server-side reformat.
        if isinstance(payload, dict) and "results" in payload:
            return payload["results"]
        return payload

    # ---- public API --------------------------------------------------------

    def list_groups(self) -> list[dict]:
        """All MTG groups (sets). ~89KB JSON, refreshed daily by TCGCSV."""
        data = self._get_json(f"/tcgplayer/{MTG_CATEGORY_ID}/groups")
        if not isinstance(data, list):
            raise TCGCSVError("TCGCSV /groups did not return a list")
        return data

    def get_prices(self, group_id: int) -> list[dict]:
        """All price rows for a single group. ~90KB JSON for a typical set.

        Each row: {productId, lowPrice, midPrice, highPrice, marketPrice,
        directLowPrice, subTypeName}. `subTypeName` ∈ {Normal, Foil, Foil Etched}.
        """
        data = self._get_json(f"/tcgplayer/{MTG_CATEGORY_ID}/{group_id}/prices")
        if not isinstance(data, list):
            raise TCGCSVError(f"TCGCSV /{group_id}/prices did not return a list")
        return data

    def last_updated(self) -> str:
        """ISO8601 timestamp of TCGCSV's most recent global refresh."""
        resp = self._request("/last-updated.txt")
        return resp.text.strip()

    # ---- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TCGCSV:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
