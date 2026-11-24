"""Bounded resource resolver tests, including signed S3 and JSON manifests."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flarefuzz_api.datasets.psi_download import download_psi_csv, download_psi_resources


@pytest.mark.anyio
async def test_resource_download_rejects_nonallowlisted_hosts_and_plain_http():
    with pytest.raises(ValueError):
        await download_psi_resources("https://example.com/data.csv")
    with pytest.raises(ValueError):
        await download_psi_resources("http://psi-repo-prod.s3.amazonaws.com/data.csv")


@pytest.mark.anyio
async def test_signed_s3_csv_is_fetched_as_resource():
    signed_url = "https://psi-repo-prod.s3.amazonaws.com/PSI-117/data.csv?X-Amz-Signature=abc%2F123"
    response = httpx.Response(200, headers={"content-type": "text/csv"}, content=b"time_s,flame_diameter_mm\n0,10\n1,11\n2,12\n")
    client = AsyncMock()
    client.__aenter__.return_value = client
    stream_context = AsyncMock()
    stream_context.__aenter__.return_value = response
    client.stream.return_value = stream_context
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        resources = await download_psi_resources(signed_url)
    assert resources[0]["url"] == signed_url
    assert resources[0]["content"] == response.content
    assert client.stream.call_args.args[1] == signed_url


@pytest.mark.anyio
async def test_json_manifest_fetches_its_s3_csv_resource_before_returning():
    manifest_url = "https://data.nasa.gov/signed-manifest.json"
    csv_url = "https://psi-repo-prod.s3.amazonaws.com/data/diameters.csv?signature=token"
    manifest = httpx.Response(200, headers={"content-type": "application/json"},
                              json={"base_url": "https://psi-repo-prod.s3.amazonaws.com/data/",
                                    "resources": [{"file_name": "diameters.csv"}]})
    csv_response = httpx.Response(200, headers={"content-type": "text/csv"},
                                  content=b"time_s,flame_diameter_mm\n0,10\n1,11\n2,12\n")
    client = AsyncMock()
    client.__aenter__.return_value = client
    def stream(_method: str, url: str):
        context = AsyncMock()
        context.__aenter__.return_value = manifest if url == manifest_url else csv_response
        return context
    client.stream.side_effect = stream
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        resources = await download_psi_resources(manifest_url)
    assert resources[0]["format"] == "csv"
    assert resources[0]["content"].startswith(b"time_s")
    requested = [call.args[1] for call in client.stream.call_args_list]
    assert manifest_url in requested
    assert "https://psi-repo-prod.s3.amazonaws.com/data/diameters.csv" in requested[1]


@pytest.mark.anyio
async def test_compatibility_csv_helper_returns_actual_csv_bytes():
    response = httpx.Response(200, headers={"content-type": "text/csv"}, content=b"a,b\n1,2\n")
    client = AsyncMock()
    client.__aenter__.return_value = client
    context = AsyncMock()
    context.__aenter__.return_value = response
    client.stream.return_value = context
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        assert await download_psi_csv("https://data.nasa.gov/data.csv") == b"a,b\n1,2\n"
