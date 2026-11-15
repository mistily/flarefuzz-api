from __future__ import annotations

"""NASA PSI discovery -> study inventory -> CSV parameter inventory.

NASA's search results identify investigations, not downloadable file URLs.
The /files/v2 response contains nested folders with file IDs and versions.
"""

from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

from flarefuzz_api.models import DatasetMetadata, GatheredInventory, InventoryResponse
from flarefuzz_api.parsers import (
    DEFAULT_PARSER_REGISTRY,
    ParserContext,
    UnsupportedFileTypeError,
)

SEARCH_URL = "https://psi.nasa.gov/geode-py/ws/repo/search"
INVESTIGATION_DATA_URL = "https://psi.nasa.gov/geode-py/ws/repo/investigations"
INVESTIGATION_PAGE_URL = "https://psi.nasa.gov/physci/repo/data/investigations"
FILES_URL = "https://psi.nasa.gov/geode-py/ws/files/v2"
STUDIES_URL = "https://psi.nasa.gov/geode-py/ws/studies"
MAX_PAGES = 5
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class GathererError(RuntimeError):
    pass


def _records(payload: Mapping) -> list[Mapping]:
    hits = payload.get("hits")
    records = hits.get("hits") if isinstance(hits, Mapping) else None
    if not isinstance(records, list):
        raise GathererError("NASA PSI search response did not contain hits.hits")
    return [item for item in records if isinstance(item, Mapping)]


def _walk_files(value: object):
    """Visit every leaf file, regardless of directory nesting depth."""
    if isinstance(value, Mapping):
        children = value.get("files")
        if isinstance(children, list):
            for child in children:
                yield from _walk_files(child)
        if value.get("id") and isinstance(value.get("key"), str):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_files(item)


def _study_version(value: object) -> int | None:
    """Extract an explicitly reported study version; never invent one."""
    if not isinstance(value, Mapping):
        return None
    for name in ("studyVersion", "StudyVersion", "version", "Version"):
        candidate = value.get(name)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
        if isinstance(candidate, str) and candidate.isdecimal() and int(candidate) > 0:
            return int(candidate)
    for name in ("study", "Study", "metadata", "Metadata"):
        nested = value.get(name)
        result = _study_version(nested)
        if result:
            return result
    return None


def _study_id(source: Mapping, record: Mapping) -> str | None:
    for name in ("studyId", "StudyId", "Accession", "accession"):
        value = source.get(name)
        if isinstance(value, str) and value.startswith("PSI-"):
            return value
    value = record.get("_id")
    if isinstance(value, str) and value.startswith("PSI-"):
        return value
    return None


async def _get_investigation(client: httpx.AsyncClient, study_id: str) -> Mapping:
    url = f"{INVESTIGATION_DATA_URL}/{quote(study_id, safe='')}"
    response = await client.get(url)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise GathererError(f"Investigation {study_id} returned non-JSON metadata") from exc
    if not isinstance(data, Mapping):
        raise GathererError(f"Investigation {study_id} metadata was not an object")
    return data


async def _inventory(client: httpx.AsyncClient, study_id: str, version: int) -> object:
    response = await client.post(
        FILES_URL,
        json={
            "fileType": "study",
            "folder": f"{study_id}/",
            "obfuscationcode": "",
            "studyId": study_id,
            "version": version,
        },
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise GathererError(f"Study {study_id} file inventory was not JSON") from exc


async def _download_csv(
    client: httpx.AsyncClient, study_id: str, study_version: int, file: Mapping
) -> tuple[str, bytes]:
    file_id = str(file["id"])
    file_version = file.get("version")
    if not isinstance(file_version, int) or file_version < 1:
        raise GathererError(f"File {file_id} has no valid file version")
    url = f"{STUDIES_URL}/{quote(study_id, safe='')}/download"
    params = {
        "file": file_id,
        "version": file_version,
        "studyVersion": study_version,
        "redirect": "false",
    }
    response = await client.get(url, params=params)
    response.raise_for_status()
    # No arbitrary redirects to other hosts. A redirect can be added after
    # confirming the permitted NASA PSI download-host contract.
    if response.is_redirect:
        raise GathererError(f"Download for {file_id} redirected unexpectedly")
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        raise GathererError(f"File {file_id} exceeds the CSV download limit")
    if response.content.lstrip().startswith((b"<html", b"<!DOCTYPE html")):
        raise GathererError(f"File {file_id} returned HTML rather than CSV")
    return str(response.url), response.content



def _links(value: object, base_url: str) -> list[str]:
    """Extract NASA PSI links from investigation metadata for the browse API.

    Retained for frontend compatibility; CSV ingestion uses /files/v2 instead.
    """
    from urllib.parse import urljoin, urlparse

    found: list[str] = []
    seen: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in ("href", "url", "downloadUrl", "download_url") and isinstance(child, str):
                    candidate = urljoin(base_url, child)
                    parsed = urlparse(candidate)
                    if parsed.scheme == "https" and parsed.hostname == "psi.nasa.gov" and candidate not in seen:
                        seen.add(candidate)
                        found.append(candidate)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found

async def gather_csv_inventories(
    *,
    term: str,
    source: str = "psi",
    resource_type: str | None = None,
    from_: int = 0,
    size: int = 10,
    sort: str | None = None,
    order: str | None = None,
    max_resources: int = 10,
) -> list[GatheredInventory]:
    results: list[GatheredInventory] = []
    visited_studies: set[str] = set()
    errors: list[str] = []
    seen_files: set[tuple[str, str]] = set()
    params: dict[str, object] = {"term": term, "source": source, "size": size}
    if resource_type:
        params["type"] = resource_type
    if sort:
        params["sort"] = sort
    if order:
        params["order"] = order

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for page in range(MAX_PAGES):
            params["from"] = from_ + page * size
            search = await client.get(SEARCH_URL, params=params)
            search.raise_for_status()
            try:
                payload = search.json()
            except ValueError as exc:
                raise GathererError("NASA PSI search returned invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise GathererError("NASA PSI search response was not an object")
            records = _records(payload)
            if not records:
                break

            for record in records:
                source_doc = record.get("_source")
                source_doc = source_doc if isinstance(source_doc, Mapping) else {}
                study_id = _study_id(source_doc, record)
                if not study_id or study_id in visited_studies:
                    continue
                visited_studies.add(study_id)
                title = source_doc.get("Title")
                try:
                    detail = await _get_investigation(client, study_id)
                    version = _study_version(detail)
                    title = detail.get("title") or title
                except (httpx.HTTPError, GathererError) as exc:
                    errors.append(f"{study_id}: investigation metadata: {exc}")
                    version = _study_version(source_doc)
                if version is None:
                    errors.append(f"{study_id}: study version not supplied by NASA metadata")
                    continue

                try:
                    tree = await _inventory(client, study_id, version)
                except (httpx.HTTPError, GathererError) as exc:
                    errors.append(f"{study_id}: file inventory: {exc}")
                    continue

                for file in _walk_files(tree):
                    key = str(file["key"])
                    if PurePosixPath(key).suffix.lower() != ".csv":
                        continue
                    if file.get("restricted") or file.get("restrictedAccess"):
                        continue
                    if file.get("visible") is False:
                        continue
                    file_id = str(file["id"])
                    if (study_id, file_id) in seen_files:
                        continue
                    seen_files.add((study_id, file_id))
                    try:
                        url, data = await _download_csv(client, study_id, version, file)
                        parsed = DEFAULT_PARSER_REGISTRY.parse(
                            data,
                            ParserContext(
                                filename=PurePosixPath(key).name,
                                media_type="text/csv",
                                metadata=DatasetMetadata(
                                    provenance="unknown",
                                    experiment=study_id,
                                    source_url=url,
                                ),
                            ),
                        )
                    except (httpx.HTTPError, GathererError, UnsupportedFileTypeError,
                            ValueError, UnicodeDecodeError) as exc:
                        errors.append(f"{study_id}/{file_id}: CSV processing: {exc}")
                        continue
                    results.append(
                        GatheredInventory(
                            project_title=str(title) if title else None,
                            project_id=study_id,
                            resource_url=url,
                            inventory=InventoryResponse(
                                metadata=parsed.metadata,
                                parameters=parsed.parameters,
                            ),
                        )
                    )
                    if len(results) >= max_resources:
                        return results
            if len(records) < size:
                break

    if not results:
        detail = "; ".join(errors[:5])
        raise GathererError(
            "No accessible NASA PSI CSV resources were parsed. "
            + (detail or "No eligible studies or CSV files found in the searched pages.")
        )
    return results
