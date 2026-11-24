"""Bounded NASA PSI/S3 resource resolver for signed URLs, JSON, and text manifests."""
from __future__ import annotations

import csv
import html
import io
import json
import re
from urllib.parse import urljoin, urlparse

import httpx

MAX_PSI_RESOURCE_BYTES = 10_000_000
_ALLOWED_HOST_SUFFIXES = ("nasa.gov",)
_ALLOWED_EXACT_HOSTS = {"psi-repo-prod.s3.amazonaws.com"}
_URL_KEYS = {"url", "href", "download_url", "downloadurl", "remote_url", "remoteurl", "s3_url", "s3url", "resource_url"}
_BASE_KEYS = {"base_url", "baseurl", "s3_base_url", "s3baseurl"}
_FILE_KEYS = {"file", "filename", "file_name", "path", "key", "resource"}
_ABSOLUTE_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_HREF_RE = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']", re.I)


def _validate_resource_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = host in _ALLOWED_EXACT_HOSTS or any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES)
    if parsed.scheme != "https" or not host or not allowed or parsed.username or parsed.password:
        raise ValueError("Only HTTPS NASA or psi-repo-prod S3 resource URLs are accepted.")
    return url


def _resource_format(url: str, content_type: str) -> str:
    media = content_type.split(";", 1)[0].strip().lower()
    path = urlparse(url).path.lower()
    if media in {"application/json", "text/json"} or path.endswith(".json"):
        return "json"
    if media in {"text/tab-separated-values", "text/tsv"} or path.endswith(".tsv"):
        return "tsv"
    if media in {"text/csv", "application/csv", "application/vnd.ms-excel"} or path.endswith(".csv"):
        return "csv"
    if media in {"text/plain", "application/octet-stream"}:
        # Plain-text responses are ambiguous. The downloaded bytes are classified
        # below as JSON, CSV, TSV, or an allowlisted link manifest.
        return "plain"
    raise ValueError(f"Unsupported NASA resource format ({media or 'unknown content type'}); supported formats are CSV, TSV, JSON, and text/plain manifests.")


def _manifest_links(payload: object, current_url: str) -> list[str]:
    links: list[str] = []
    bases: list[str] = []
    file_refs: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                normalized = str(child_key).casefold().replace("-", "_")
                if normalized in _BASE_KEYS and isinstance(child_value, str):
                    bases.append(child_value)
                elif normalized in _URL_KEYS and isinstance(child_value, str):
                    links.append(child_value)
                elif normalized in _FILE_KEYS and isinstance(child_value, str):
                    file_refs.append(child_value)
                visit(child_value)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    resolved_links: list[str] = []
    for link in links:
        resolved_links.append(urljoin(current_url, html.unescape(link)))
    for base in bases:
        absolute_base = urljoin(current_url, html.unescape(base).rstrip("/") + "/")
        for file_ref in file_refs:
            resolved_links.append(urljoin(absolute_base, html.unescape(file_ref).lstrip("/")))
    return list(dict.fromkeys(link for link in resolved_links if link != current_url))


def _plain_payload_kind(content: bytes, current_url: str) -> tuple[str, object | None]:
    """Digest text/plain S3 responses into JSON, table, or link-manifest forms."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "unsupported", None
    stripped = text.strip()
    if not stripped:
        return "unsupported", None

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        return "json", parsed

    # A text/plain endpoint may contain an HTML fragment or a newline/Markdown link list.
    links = [html.unescape(match) for match in _HREF_RE.findall(stripped)]
    links.extend(html.unescape(match) for match in _ABSOLUTE_URL_RE.findall(stripped))
    resolved: list[str] = []
    for line in stripped.splitlines():
        line = line.strip().strip("[](),<>\"'")
        if not line:
            continue
        markdown = re.search(r"\((https?://[^)]+)\)", line)
        if markdown:
            links.append(markdown.group(1))
        elif re.fullmatch(r"[^\s]+", line) and not any(ch in line for ch in (",", "\t")):
            if "/" in line or line.lower().endswith((".csv", ".tsv", ".json")):
                resolved.append(urljoin(current_url, line.lstrip("/")))

    for link in links:
        resolved.append(urljoin(current_url, link))
    if resolved:
        return "links", list(dict.fromkeys(resolved))

    # CSV/TSV sent with text/plain remains valid if extension or table syntax identifies it.
    try:
        first_line = stripped.splitlines()[0]
        if "\t" in first_line:
            return "tsv", None
        dialect = csv.Sniffer().sniff(first_line, delimiters=",;")
        if dialect.delimiter in {",", ";"}:
            return "csv", None
    except csv.Error:
        pass
    return "unsupported", None


async def download_psi_resources(url: str) -> list[dict[str, object]]:
    """Fetch and digest NASA/S3 resources. Signed query strings are preserved."""
    initial = _validate_resource_url(url)
    resources: list[dict[str, object]] = []
    visited: set[str] = set()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False) as client:
        async def fetch(resource_url: str, depth: int = 0) -> None:
            resource_url = _validate_resource_url(resource_url)
            if resource_url in visited:
                return
            if depth > 3 or len(visited) >= 20:
                raise ValueError("NASA resource manifest nesting/resource limit exceeded.")
            visited.add(resource_url)
            async with client.stream("GET", resource_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                kind = _resource_format(resource_url, content_type)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PSI_RESOURCE_BYTES:
                        raise ValueError("NASA PSI resource exceeds the 10 MB limit.")
                    chunks.append(chunk)
                content = b"".join(chunks)

            if kind == "plain":
                plain_kind, plain_payload = _plain_payload_kind(content, resource_url)
                kind = plain_kind if plain_kind in {"csv", "tsv", "json", "links"} else "unsupported"
                if kind == "json":
                    children = _manifest_links(plain_payload, resource_url)
                elif kind == "links":
                    children = plain_payload if isinstance(plain_payload, list) else []
                else:
                    children = []
                if kind in {"json", "links"}:
                    if not children:
                        raise ValueError("NASA text/plain response did not contain recognized resource links.")
                    for child_url in children:
                        await fetch(str(child_url), depth + 1)
                    return
            elif kind == "json":
                try:
                    manifest = json.loads(content.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("NASA JSON resource is not a valid UTF-8 manifest.") from exc
                children = _manifest_links(manifest, resource_url)
                if not children:
                    raise ValueError("NASA JSON response did not contain a recognized resource link.")
                for child_url in children:
                    await fetch(child_url, depth + 1)
                return
            if kind == "unsupported":
                raise ValueError("Unsupported NASA text/plain resource: expected JSON, CSV/TSV, or an allowlisted link manifest.")
            name = urlparse(resource_url).path.rsplit("/", 1)[-1] or "resource"
            resources.append({"url": resource_url, "filename": name, "format": kind,
                              "content_type": content_type, "content": content})

        await fetch(initial)
    if not resources:
        raise ValueError("NASA resource did not resolve to a supported data file.")
    return resources


async def download_psi_csv(url: str) -> bytes:
    """Compatibility helper: resolve a URL/manifest and require exactly one CSV."""
    resources = await download_psi_resources(url)
    csv_resources = [resource for resource in resources if resource["format"] == "csv"]
    if len(csv_resources) != 1 or len(resources) != 1:
        raise ValueError("Expected exactly one CSV resource; use download_psi_resources for manifests or multiple files.")
    return csv_resources[0]["content"]  # type: ignore[return-value]
