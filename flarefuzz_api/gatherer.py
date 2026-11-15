from __future__ import annotations

import re
from json import JSONDecodeError
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from flarefuzz_api.models import DatasetMetadata, GatheredInventory, InventoryResponse
from flarefuzz_api.parsers import (
    DEFAULT_PARSER_REGISTRY,
    ParserContext,
    UnsupportedFileTypeError,
)

SEARCH_URL = "https://psi.nasa.gov/geode-py/ws/repo/search"
INVESTIGATION_DATA_URL = "https://psi.nasa.gov/geode-py/ws/repo/investigations"
_NASA_DOMAIN = "nasa.gov"
_MAX_LINK_DEPTH = 3
_RESOURCE_CONTEXTS = ("project", "investigation", "resource", "dataset", "data", "file", "download")


class GathererError(RuntimeError):
    pass


def _nasa_url(value: str, base_url: str) -> str | None:
    value = value.strip()
    url = urljoin(base_url, value)
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (hostname == _NASA_DOMAIN or hostname.endswith(f".{_NASA_DOMAIN}")):
        return None
    return url


def _links(value: object, base_url: str, context: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        relation = str(value.get("rel", "")).lower()
        for key, child in value.items():
            normalized_key = key.lower().replace("_", "")
            child_context = f"{context} {key.lower()}"
            if normalized_key in {"link", "links", "url", "href", "downloadurl"}:
                children = child if isinstance(child, Sequence) and not isinstance(child, (str, bytes)) else [child]
                is_resource = any(token in child_context for token in _RESOURCE_CONTEXTS)
                is_project_relation = any(
                    token in relation for token in ("project", "investigation", "resource", "dataset", "data", "file")
                )
                if is_resource or is_project_relation or normalized_key == "downloadurl":
                    for item in children:
                        if isinstance(item, str):
                            candidates = re.findall(r"https://[^\s\"<>]+|/(?:geode-py/)?[^\s\"<>]+", item)
                            if not candidates:
                                candidates = [item]
                            for candidate in candidates:
                                url = _nasa_url(candidate, base_url)
                                if url:
                                    found.append(url)
                        elif isinstance(item, Mapping):
                            found.extend(_links(item, base_url, child_context))
            else:
                found.extend(_links(child, base_url, child_context))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found.extend(_links(child, base_url, context))
    return list(dict.fromkeys(found))


def _records(search_json: Mapping[str, object]) -> list[Mapping[str, object]]:
    hits = search_json.get("hits")
    entries = hits.get("hits") if isinstance(hits, Mapping) else None
    if not isinstance(entries, list):
        raise GathererError("NASA PSI search response did not contain hits.hits.")
    return [entry for entry in entries if isinstance(entry, Mapping)]


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
    params: dict[str, object] = {"term": term, "source": source, "from": from_, "size": size}
    if resource_type:
        params["type"] = resource_type
    if sort:
        params["sort"] = sort
    if order:
        params["order"] = order

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(SEARCH_URL, params=params)
        response.raise_for_status()
        try:
            search_json = response.json()
        except ValueError as exc:
            raise GathererError("NASA PSI search returned invalid JSON.") from exc
        if not isinstance(search_json, Mapping):
            raise GathererError("NASA PSI search response was not a JSON object.")
        results: list[GatheredInventory] = []

        search_records = _records(search_json)
        for record in search_records:
            source_doc = record.get("_source")
            source_doc = source_doc if isinstance(source_doc, Mapping) else {}
            project_id = str(source_doc.get("Accession") or record.get("_id") or "")
            title = source_doc.get("Title")
            seen: set[str] = set()
            queue = [(url, 0) for url in _links(source_doc, SEARCH_URL)]
            if not queue and source_doc.get("Accession"):
                project_url = _nasa_url(
                    f"{INVESTIGATION_DATA_URL}/{quote(str(source_doc['Accession']), safe='')}",
                    SEARCH_URL,
                )
                if project_url:
                    queue.append((project_url, 0))
            while queue:
                resource_url, depth = queue.pop(0)
                if resource_url in seen:
                    continue
                seen.add(resource_url)
                try:
                    resource = await client.get(resource_url)
                    resource.raise_for_status()
                except httpx.HTTPError:
                    # A broken resource link must not abort discovery of other files.
                    continue
                content_type = resource.headers.get("content-type", "").lower()
                disposition = resource.headers.get("content-disposition", "").lower()
                disposition_name = re.search(r'filename="?([^";]+)', disposition)
                filename = (
                    disposition_name.group(1).strip()
                    if disposition_name
                    else Path(urlparse(resource_url).path).name
                )
                context = ParserContext(
                    filename=filename,
                    media_type=content_type,
                    metadata=DatasetMetadata(
                        provenance="unknown",
                        source_url=str(resource.url),
                    ),
                )
                try:
                    parsed = DEFAULT_PARSER_REGISTRY.parse(resource.content, context)
                except UnsupportedFileTypeError:
                    if depth < _MAX_LINK_DEPTH and "json" in content_type:
                        try:
                            linked_json = resource.json()
                        except (ValueError, JSONDecodeError) as exc:
                            raise GathererError(
                                f"Invalid NASA PSI JSON resource at {resource.url}."
                            ) from exc
                        queue.extend(
                            (url, depth + 1)
                            for url in _links(linked_json, str(resource.url))
                        )
                    continue
                except (ValueError, UnicodeDecodeError) as exc:
                    raise GathererError(f"Could not parse NASA PSI CSV at {resource.url}: {exc}") from exc
                else:
                    results.append(
                        GatheredInventory(
                            project_title=str(title) if title else None,
                            project_id=project_id or None,
                            resource_url=str(resource.url),
                            inventory=InventoryResponse(
                                metadata=parsed.metadata,
                                parameters=parsed.parameters,
                            ),
                        )
                    )
                    if len(results) >= max_resources:
                        return results
        if not results:
            raise GathererError(
                f"NASA PSI returned {len(search_records)} search records but no accessible "
                "NASA-hosted CSV resources were discovered."
            )
    return results
