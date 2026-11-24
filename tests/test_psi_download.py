from unittest.mock import AsyncMock, patch

import pytest

from flarefuzz_api.datasets.psi_download import download_psi_csv


@pytest.mark.anyio
async def test_psi_download_rejects_non_nasa_and_plain_http():
    with pytest.raises(ValueError):
        await download_psi_csv("https://example.com/data.csv")
    with pytest.raises(ValueError):
        await download_psi_csv("http://nasa.gov/data.csv")


@pytest.mark.anyio
async def test_psi_download_uses_nasa_host_allowlist():
    response = type("Response", (), {"headers": {"content-type": "text/csv"}, "content": b"time_s,flame_diameter_mm\n", "raise_for_status": lambda self: None})()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = response
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        assert await download_psi_csv("https://data.nasa.gov/data.csv") == b"time_s,flame_diameter_mm\n"
