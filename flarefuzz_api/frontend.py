from __future__ import annotations

from math import isfinite, sqrt
from statistics import mean
from typing import Literal
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from flarefuzz_api.gatherer import INVESTIGATION_DATA_URL, SEARCH_URL
from flarefuzz_api.models import DatasetMetadata, NormalizedCSVResponse, Provenance
from flarefuzz_api.parsers import (
    DEFAULT_PARSER_REGISTRY,
    ParserContext,
    UnsupportedFileTypeError,
)

router = APIRouter(prefix="/api")

# MVP storage: datasets and runs are process-local. Deployments requiring persistence
# should replace these stores with a database/object store implementation.
_DATASETS: dict[str, NormalizedCSVResponse] = {}
_RUNS: dict[str, dict[str, object]] = {}


class FitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dataset_id: str
    model: Literal["linear"] = "linear"
    x_series: str
    y_series: str


class QuestionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dataset_id: str
    question: str = Field(min_length=1, max_length=2000)


def _dataset_or_404(dataset_id: str) -> NormalizedCSVResponse:
    dataset = _DATASETS.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return dataset


def _psi_record(record: object) -> dict[str, object] | None:
    if not isinstance(record, dict):
        return None
    source = record.get("_source")
    source = source if isinstance(source, dict) else {}
    identifier = source.get("Accession") or record.get("_id")
    if identifier is None:
        return None
    return {
        "id": str(identifier),
        "title": source.get("Title"),
        "description": source.get("Description") or source.get("Abstract"),
        "metadata": source,
    }


@router.get("/investigations")
async def browse_investigations(
    term: str = Query("combustion", min_length=1, max_length=200),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, object]:
    """Search NASA PSI investigations for the frontend browse view."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False
        ) as client:
            response = await client.get(
                SEARCH_URL,
                params={"term": term, "source": "psi", "from": offset, "size": limit},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc

    hits = payload.get("hits", {}) if isinstance(payload, dict) else {}
    entries = hits.get("hits", []) if isinstance(hits, dict) else []
    investigations = [item for item in (_psi_record(hit) for hit in entries) if item]
    total = hits.get("total") if isinstance(hits, dict) else None
    return {"items": investigations, "offset": offset, "limit": limit, "total": total}


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: str) -> dict[str, object]:
    """Return canonical PSI JSON metadata and its directly referenced files."""
    import re
    from flarefuzz_api.gatherer import INVESTIGATION_PAGE_URL, _get_investigation

    if not re.fullmatch(r"PSI-[0-9]+", investigation_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="Invalid PSI investigation ID.")
    investigation_id = investigation_id.upper()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False
        ) as client:
            metadata = dict(await _get_investigation(client, investigation_id))
    except httpx.HTTPStatusError as exc:
        status = 404 if exc.response.status_code == 404 else 502
        raise HTTPException(status_code=status, detail="NASA PSI investigation lookup failed.") from exc
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc

    accession = metadata.get("accession")
    if accession != investigation_id:
        raise HTTPException(status_code=502, detail="NASA PSI returned a different investigation.")
    source_files = []
    for category in ("experimentalTable", "images", "modified"):
        entries = metadata.get(category)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("restricted") or entry.get("visible") is False:
                continue
            source_files.append({
                "category": category,
                "file_name": entry.get("file_name"),
                "file_version": entry.get("version"),
                "remote_url": entry.get("remote_url"),
                "s3_path": entry.get("s3_path"),
            })
    return {
        "id": investigation_id,
        "title": metadata.get("title"),
        "description": metadata.get("description") or metadata.get("approach"),
        "metadata": metadata,
        "source_files": source_files,
        "page_url": f"{INVESTIGATION_PAGE_URL}/{quote(investigation_id, safe='')}",
    }


@router.post("/datasets/ingest", status_code=201)
async def ingest_dataset(
    file: UploadFile = File(...),
    provenance: Provenance = Form(Provenance.UNKNOWN),
    experiment: str | None = Form(None),
    simulation: str | None = Form(None),
    header_rows: int | None = Form(None, ge=1),
) -> dict[str, object]:
    """Parse a CSV and retain its normalized values for follow-up API calls."""
    try:
        parsed = DEFAULT_PARSER_REGISTRY.parse(
            await file.read(),
            ParserContext(
                filename=file.filename,
                media_type=file.content_type,
                metadata=DatasetMetadata(
                    provenance=provenance,
                    experiment=experiment,
                    simulation=simulation,
                ),
                options={"header_rows": header_rows} if header_rows is not None else {},
            ),
        )
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    dataset_id = str(uuid4())
    _DATASETS[dataset_id] = parsed
    return {
        "id": dataset_id,
        "metadata": parsed.metadata,
        "parameters": parsed.parameters,
        "row_count": len(parsed.si_values),
        "quality_issue_count": len(parsed.validation_issues),
    }


@router.get("/datasets/{dataset_id}/quality")
def dataset_quality(dataset_id: str) -> dict[str, object]:
    dataset = _dataset_or_404(dataset_id)
    return {
        "dataset_id": dataset_id,
        "issues": dataset.validation_issues,
        "unit_mappings": [
            {
                "series_identifier": parameter.series_identifier,
                "source_header": parameter.source_header,
                "canonical_name": parameter.canonical_name,
                "source_unit": parameter.source_unit,
                "si_unit": parameter.si_unit,
                "conversion_factor": parameter.conversion_factor,
                "validation_status": parameter.validation_status,
                "notes": parameter.notes,
            }
            for parameter in dataset.parameters
        ],
    }


@router.get("/datasets/{dataset_id}/series")
def dataset_series(dataset_id: str) -> dict[str, object]:
    dataset = _dataset_or_404(dataset_id)
    return {
        "dataset_id": dataset_id,
        "metadata": dataset.metadata,
        "parameters": dataset.parameters,
        "row_count": len(dataset.si_values),
        "series": [
            {
                "series_identifier": parameter.series_identifier,
                "unit": parameter.si_unit or parameter.source_unit,
                "values": [row.get(parameter.series_identifier) for row in dataset.si_values],
            }
            for parameter in dataset.parameters
        ],
    }


@router.post("/models/fit", status_code=201)
def fit_model(request: FitRequest) -> dict[str, object]:
    """Fit an explicit ordinary-least-squares line to two normalized series."""
    dataset = _dataset_or_404(request.dataset_id)
    available = {parameter.series_identifier for parameter in dataset.parameters}
    if request.x_series not in available or request.y_series not in available:
        raise HTTPException(status_code=422, detail="x_series and y_series must name dataset series.")

    pairs: list[tuple[int, float, float]] = []
    for index, row in enumerate(dataset.si_values):
        x_value, y_value = row.get(request.x_series), row.get(request.y_series)
        if x_value is not None and y_value is not None and isfinite(x_value) and isfinite(y_value):
            pairs.append((index, x_value, y_value))
    if len(pairs) < 3:
        raise HTTPException(status_code=422, detail="At least three complete finite point pairs are required.")

    xs, ys = [pair[1] for pair in pairs], [pair[2] for pair in pairs]
    x_mean, y_mean = mean(xs), mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        raise HTTPException(status_code=422, detail="The x series must contain at least two distinct values.")
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - slope * x_mean
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    residual_standard_error = sqrt(sum(residual * residual for residual in residuals) / (len(pairs) - 2))
    x_parameter = next(p for p in dataset.parameters if p.series_identifier == request.x_series)
    y_parameter = next(p for p in dataset.parameters if p.series_identifier == request.y_series)
    run_id = str(uuid4())
    result: dict[str, object] = {
        "id": run_id,
        "dataset_id": request.dataset_id,
        "model": "linear",
        "equation": f"{request.y_series} = {slope} * {request.x_series} + {intercept}",
        "x_series": request.x_series,
        "y_series": request.y_series,
        "x_unit": x_parameter.si_unit or x_parameter.source_unit,
        "y_unit": y_parameter.si_unit or y_parameter.source_unit,
        "sample_count": len(pairs),
        "slope": slope,
        "intercept": intercept,
        "residual_standard_error": residual_standard_error,
        "uncertainty_95_approx": 1.96 * residual_standard_error,
        "points": [
            {
                "row_index": pair[0],
                "x": pair[1],
                "observed": pair[2],
                "predicted": slope * pair[1] + intercept,
                "residual": residual,
            }
            for pair, residual in zip(pairs, residuals)
        ],
        "method": "ordinary least squares; uncertainty is 1.96 times residual standard error",
    }
    _RUNS[run_id] = result
    return result


@router.get("/models/runs/{run_id}")
def get_model_run(run_id: str) -> dict[str, object]:
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Model run not found.")
    return run


@router.post("/questions")
def ask_question(request: QuestionRequest) -> dict[str, object]:
    """Answer only with deterministic summaries of uploaded dataset evidence."""
    dataset = _dataset_or_404(request.dataset_id)
    normalized_question = request.question.casefold()
    matches = [
        parameter for parameter in dataset.parameters
        if parameter.series_identifier.casefold() in normalized_question
        or (parameter.canonical_name and parameter.canonical_name.replace("_", " ") in normalized_question)
        or parameter.source_header.casefold() in normalized_question
    ]
    if not matches:
        return {
            "answer": "I can't answer that from the supplied dataset evidence. Mention a series name or canonical parameter to request a grounded summary.",
            "grounded": False,
            "evidence": [],
            "available_series": [p.series_identifier for p in dataset.parameters],
        }

    evidence: list[dict[str, object]] = []
    summaries: list[str] = []
    for parameter in matches:
        values = [
            (index, row.get(parameter.series_identifier))
            for index, row in enumerate(dataset.si_values)
            if row.get(parameter.series_identifier) is not None
            and isfinite(row[parameter.series_identifier])
        ]
        if not values:
            continue
        numbers = [value for _, value in values]
        average = mean(numbers)
        evidence.append({
            "dataset_id": request.dataset_id,
            "series_identifier": parameter.series_identifier,
            "unit": parameter.si_unit or parameter.source_unit,
            "row_indices": [index for index, _ in values],
            "count": len(values),
            "minimum": min(numbers),
            "maximum": max(numbers),
            "mean": average,
        })
        summaries.append(
            f"{parameter.series_identifier}: {len(values)} finite normalized values; "
            f"minimum {min(numbers)}, maximum {max(numbers)}, mean {average} "
            f"{parameter.si_unit or parameter.source_unit or '(unit unknown)'}."
        )
    if not evidence:
        return {"answer": "No finite normalized values are available for the requested series.", "grounded": False, "evidence": []}
    return {
        "answer": " ".join(summaries),
        "grounded": True,
        "evidence": evidence,
        "limitations": "This endpoint reports descriptive statistics only; it does not infer causation or generate literature-backed claims.",
    }
