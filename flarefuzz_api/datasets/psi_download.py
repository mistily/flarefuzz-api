"""Bounded NASA PSI CSV downloader. Arbitrary hosts and redirects are rejected."""
from __future__ import annotations
from urllib.parse import urlparse

import httpx

MAX_PSI_CSV_BYTES = 10_000_000
_ALLOWED_HOST_SUFFIXES = ("nasa.gov",)


async def download_psi_csv(url: str) -> bytes:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or not any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        raise ValueError("Only HTTPS URLs hosted on a NASA domain are accepted.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_PSI_CSV_BYTES:
            raise ValueError("NASA PSI CSV exceeds the 10 MB download limit.")
        content_type = response.headers.get("content-type", "").lower()
        if "text/csv" not in content_type and "application/csv" not in content_type and not url.lower().endswith(".csv"):
            raise ValueError("NASA PSI resource does not appear to be a CSV file.")
        return response.content
