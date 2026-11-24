"""Tests for signed S3, JSON, and text/plain NASA resource responses."""
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
    assert resources[0]["format"] == "csv"
    assert resources[0]["content"] == response.content


@pytest.mark.anyio
async def test_plain_text_link_list_fetches_link_contents():
    manifest_url = "https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/"
    csv_url = "https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/Burning_history.csv"
    manifest = httpx.Response(200, headers={"content-type": "text/plain"},
                              content=("https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/Burning_history.csv\n"
                                       "https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/Dext.csv\n").encode())
    def response_for(url: str) -> httpx.Response:
        if url == manifest_url:
            return manifest
        return httpx.Response(200, headers={"content-type": "text/csv"},
                              content=b"time_s,flame_diameter_mm\n0,10\n1,11\n2,12\n")
    client = AsyncMock()
    client.__aenter__.return_value = client
    def stream(_method: str, url: str):
        context = AsyncMock()
        context.__aenter__.return_value = response_for(url)
        return context
    client.stream.side_effect = stream
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        resources = await download_psi_resources(manifest_url)
    assert len(resources) == 2
    assert all(resource["format"] == "csv" for resource in resources)
    assert csv_url in [resource["url"] for resource in resources]


@pytest.mark.anyio
async def test_plain_text_relative_links_use_the_response_base_url():
    manifest_url = "https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/index.txt"
    manifest = httpx.Response(200, headers={"content-type": "text/plain"},
                              content=b"Burning_history.csv\nDext.csv\n")
    client = AsyncMock()
    client.__aenter__.return_value = client
    def stream(_method: str, url: str):
        context = AsyncMock()
        if url == manifest_url:
            context.__aenter__.return_value = manifest
        else:
            context.__aenter__.return_value = httpx.Response(200, headers={"content-type": "text/csv"},
                                                              content=b"time_s,flame_diameter_mm\n0,10\n1,11\n2,12\n")
        return context
    client.stream.side_effect = stream
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        resources = await download_psi_resources(manifest_url)
    assert len(resources) == 2
    assert resources[0]["url"] == "https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/Burning_history.csv"


@pytest.mark.anyio
async def test_json_manifest_still_resolves_s3_resources():
    manifest_url = "https://data.nasa.gov/signed-manifest.json"
    csv_url = "https://psi-repo-prod.s3.amazonaws.com/data/diameters.csv"
    manifest = httpx.Response(200, headers={"content-type": "application/json"},
                              json={"base_url": "https://psi-repo-prod.s3.amazonaws.com/data/",
                                    "resources": [{"file_name": "diameters.csv"}]})
    client = AsyncMock()
    client.__aenter__.return_value = client
    def stream(_method: str, url: str):
        context = AsyncMock()
        context.__aenter__.return_value = manifest if url == manifest_url else httpx.Response(
            200, headers={"content-type": "text/csv"}, content=b"time_s,flame_diameter_mm\n0,10\n1,11\n2,12\n")
        return context
    client.stream.side_effect = stream
    with patch("flarefuzz_api.datasets.psi_download.httpx.AsyncClient", return_value=client):
        resources = await download_psi_resources(manifest_url)
    assert resources[0]["url"] == csv_url
