from __future__ import annotations

from math import isfinite, sqrt
from statistics import mean
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from flarefuzz_api.gatherer import INVESTIGATION_DATA_URL, SEARCH_URL
from flarefuzz_api.models import DatasetMetadata, NormalizedCSVResponse, Provenance
from flarefuzz_api.parsers import DEFAULT_PARSER_REGISTRY, ParserContext, UnsupportedFileTypeError
from flarefuzz_api.simulation.fitting import fit_quadratic_diameter

router = APIRouter(prefix="/api")

# MVP stores are process-local. Replace with persistent storage for multi-worker use.
_DATASETS: dict[str, NormalizedCSVResponse] = {}
_RUNS: dict[str, dict[str, object]] = {}


class UncertaintyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    method: Literal["bootstrap", "covariance"] = "bootstrap"
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0)
    iterations: int = Field(default=500, ge=50, le=2000)


class FitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dataset_id: str
    model: Literal["linear", "quadratic"] = "linear"
    x_series: str | None = None
    y_series: str | None = None
    experiment_id: str | None = None
    investigation_id: str | None = None
    target: Literal["flame_diameter_m"] = "flame_diameter_m"
    uncertainty: UncertaintyRequest = Field(default_factory=UncertaintyRequest)
    prediction_points: int = Field(default=101, ge=3, le=1000)
    extrapolation_s: float = Field(default=0.0, ge=0, allow_inf_nan=False)


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
    return {"id": str(identifier), "title": source.get("Title"),
            "description": source.get("Description") or source.get("Abstract"), "metadata": source}


@router.get("/investigations")
async def browse_investigations(term: str = Query("combustion", min_length=1, max_length=200),
                                offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> dict[str, object]:
    """Search NASA PSI investigations for the frontend browse view."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False) as client:
            response = await client.get(SEARCH_URL, params={"term": term, "source": "psi", "from": offset, "size": limit})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc
    hits = payload.get("hits", {}) if isinstance(payload, dict) else {}
    entries = hits.get("hits", []) if isinstance(hits, dict) else []
    investigations = [item for item in (_psi_record(hit) for hit in entries) if item]
    return {"items": investigations, "offset": offset, "limit": limit,
            "total": hits.get("total") if isinstance(hits, dict) else None}


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: str) -> dict[str, object]:
    """Return canonical PSI JSON metadata and its directly referenced files."""
    import re
    from flarefuzz_api.gatherer import INVESTIGATION_PAGE_URL, _get_investigation
    if not re.fullmatch(r"PSI-[0-9]+", investigation_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="Invalid PSI investigation ID.")
    investigation_id = investigation_id.upper()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False) as client:
            metadata = dict(await _get_investigation(client, investigation_id))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=404 if exc.response.status_code == 404 else 502,
                            detail="NASA PSI investigation lookup failed.") from exc
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc
    if metadata.get("accession") != investigation_id:
        raise HTTPException(status_code=502, detail="NASA PSI returned a different investigation.")
    source_files = []
    for category in ("experimentalTable", "images", "modified"):
        for entry in metadata.get(category, []) if isinstance(metadata.get(category), list) else []:
            if isinstance(entry, dict) and not entry.get("restricted") and entry.get("visible") is not False:
                source_files.append({"category": category, "file_name": entry.get("file_name"),
                                     "file_version": entry.get("version"), "remote_url": entry.get("remote_url"),
                                     "s3_path": entry.get("s3_path")})
    return {"id": investigation_id, "title": metadata.get("title"),
            "description": metadata.get("description") or metadata.get("approach"), "metadata": metadata,
            "source_files": source_files,
            "page_url": f"{INVESTIGATION_PAGE_URL}/{quote(investigation_id, safe='')}"}


@router.post("/datasets/ingest", status_code=201)
async def ingest_dataset(file: UploadFile = File(...), provenance: Provenance = Form(Provenance.UNKNOWN),
                         experiment: str | None = Form(None), simulation: str | None = Form(None),
                         header_rows: int | None = Form(None, ge=1)) -> dict[str, object]:
    """Parse CSV and retain normalized values for follow-up calls."""
    try:
        parsed = DEFAULT_PARSER_REGISTRY.parse(await file.read(), ParserContext(
            filename=file.filename, media_type=file.content_type,
            metadata=DatasetMetadata(provenance=provenance, experiment=experiment, simulation=simulation),
            options={"header_rows": header_rows} if header_rows is not None else {}))
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    dataset_id = str(uuid4())
    _DATASETS[dataset_id] = parsed
    return {"id": dataset_id, "metadata": parsed.metadata, "parameters": parsed.parameters,
            "row_count": len(parsed.si_values), "quality_issue_count": len(parsed.validation_issues)}


@router.get("/datasets/{dataset_id}/quality")
def dataset_quality(dataset_id: str) -> dict[str, object]:
    dataset = _dataset_or_404(dataset_id)
    return {"dataset_id": dataset_id, "issues": dataset.validation_issues,
            "unit_mappings": [{"series_identifier": p.series_identifier, "source_header": p.source_header,
                "canonical_name": p.canonical_name, "source_unit": p.source_unit, "si_unit": p.si_unit,
                "conversion_factor": p.conversion_factor, "validation_status": p.validation_status, "notes": p.notes}
                for p in dataset.parameters]}


@router.get("/datasets/{dataset_id}/series")
def dataset_series(dataset_id: str) -> dict[str, object]:
    dataset = _dataset_or_404(dataset_id)
    return {"dataset_id": dataset_id, "metadata": dataset.metadata, "parameters": dataset.parameters,
            "row_count": len(dataset.si_values),
            "series": [{"series_identifier": p.series_identifier, "unit": p.si_unit or p.source_unit,
                        "values": [row.get(p.series_identifier) for row in dataset.si_values]} for p in dataset.parameters]}


def _fit_quadratic(request: FitRequest) -> dict[str, object]:
    dataset = _dataset_or_404(request.dataset_id)
    if not request.experiment_id:
        raise HTTPException(status_code=422, detail="experiment_id is required for a quadratic diameter fit; upload one experiment per dataset.")
    by_name = {p.series_identifier: p for p in dataset.parameters}
    time_parameters = [p for p in dataset.parameters if p.canonical_name == "time" and p.si_unit == "s"]
    diameter_parameters = [p for p in dataset.parameters if p.canonical_name == "flame_diameter" and p.si_unit == "m"]
    if len(time_parameters) != 1 or len(diameter_parameters) != 1:
        raise HTTPException(status_code=422, detail="Dataset must have exactly one recognized time series in seconds and flame_diameter series in meters.")
    tp, dp = time_parameters[0], diameter_parameters[0]
    if tp.validation_status != "validated" or dp.validation_status != "validated":
        raise HTTPException(status_code=422, detail="Time and flame diameter columns must have validated identities and units.")
    pairs = [(float(row[tp.series_identifier]), float(row[dp.series_identifier]))
             for row in dataset.si_values if row.get(tp.series_identifier) is not None and row.get(dp.series_identifier) is not None
             and isfinite(row[tp.series_identifier]) and isfinite(row[dp.series_identifier])]
    if len(pairs) < 3:
        raise HTTPException(status_code=422, detail="At least three complete finite time/diameter pairs are required.")
    t = np.asarray([pair[0] for pair in pairs], dtype=float)
    d = np.asarray([pair[1] for pair in pairs], dtype=float)
    if len(np.unique(t)) < 3:
        raise HTTPException(status_code=422, detail="Quadratic fitting requires three distinct time values.")
    try:
        fit = fit_quadratic_diameter(t.tolist(), d.tolist())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    origin = fit.origin_time_s
    x = t - origin
    design = np.column_stack((np.ones_like(x), x, x * x))
    prediction_end = float(t.max() + request.extrapolation_s)
    prediction_times = np.linspace(float(t.min()), prediction_end, request.prediction_points)
    xp = prediction_times - origin
    prediction_design = np.column_stack((np.ones_like(xp), xp, xp * xp))
    coefficients = np.asarray(fit.coefficients)
    central = prediction_design @ coefficients
    residuals = d - design @ coefficients
    confidence = request.uncertainty.confidence_level
    alpha = (1.0 - confidence) / 2.0
    if request.uncertainty.method == "bootstrap":
        rng = np.random.default_rng(2026)
        fitted_values = design @ coefficients
        bootstrap_predictions = []
        for _ in range(request.uncertainty.iterations):
            sampled = rng.choice(residuals, size=len(residuals), replace=True)
            boot_y = fitted_values + sampled
            boot_coefficients = np.linalg.lstsq(design, boot_y, rcond=None)[0]
            bootstrap_predictions.append(prediction_design @ boot_coefficients)
        boot = np.asarray(bootstrap_predictions)
        lower, upper = np.quantile(boot, [alpha, 1.0 - alpha], axis=0)
        uncertainty_method = f"residual bootstrap ({request.uncertainty.iterations} iterations); pointwise percentile interval"
    else:
        covariance = np.asarray(fit.covariance)
        variances = np.einsum("ij,jk,ik->i", prediction_design, covariance, prediction_design)
        # Normal quantile approximation is accurate enough for this descriptive MVP.
        z = 1.959963984540054 if confidence == 0.95 else 1.959963984540054
        half_width = z * np.sqrt(np.maximum(variances, 0.0) + fit.residual_scale_m**2)
        lower, upper = central - half_width, central + half_width
        uncertainty_method = "parameter covariance plus residual scale; approximate pointwise normal interval"
    run_id = str(uuid4())
    limits = ["Empirical model, not a CFD solution"]
    if request.extrapolation_s == 0:
        limits.append("Prediction restricted to fitted time interval")
    else:
        limits.append("Extrapolated predictions are not validated")
    result: dict[str, object] = {
        "run_id": run_id, "status": "completed", "model": "quadratic", "data_kind": "model_prediction",
        "source": {"investigation_id": request.investigation_id, "dataset_id": request.dataset_id,
                   "experiment_id": request.experiment_id, "source_url": dataset.metadata.source_url},
        "parameters": {"D0_m": float(coefficients[0]), "a_m_per_s": float(coefficients[1]),
                       "b_m_per_s2": float(coefficients[2]), "time_origin_s": origin},
        "metrics": {"rmse_m": fit.rmse_m, "sample_count": fit.sample_count},
        "predictions": {"timeS": prediction_times.tolist(), "flameDiameterM": central.tolist(),
                        "lowerFlameDiameterM": np.maximum(0.0, lower).tolist(), "upperFlameDiameterM": upper.tolist()},
        "uncertainty": {"method": uncertainty_method, "confidence_level": confidence,
                        "iterations": request.uncertainty.iterations if request.uncertainty.method == "bootstrap" else None},
        "diagnostics": {"origin_time_s": origin, "observed_time_range_s": [float(t.min()), float(t.max())],
                        "source_row_indices": [index for index, row in enumerate(dataset.si_values)
                                               if row.get(tp.series_identifier) is not None and row.get(dp.series_identifier) is not None]},
        "units": {"time": "s", "diameter": "m", "a": "m/s", "b": "m/s^2"},
        "limitations": limits + ["Quadratic empirical trend is not a universal combustion law; intervals do not account for model misspecification."],
        "assumptions": ["One uploaded dataset corresponds to the requested experiment; runs are not combined.",
                        "D(t)=D0+a*(t-t_origin)+b*(t-t_origin)^2 is an empirical model fit within the observed interval.",
                        "No physical coupling to Cantera properties or environment is implied."],
    }
    _RUNS[run_id] = result
    return result


@router.post("/models/fit", status_code=201)
def fit_model(request: FitRequest) -> dict[str, object]:
    """Fit a legacy linear model or empirical quadratic flame diameter curve."""
    if request.model == "quadratic":
        return _fit_quadratic(request)
    dataset = _dataset_or_404(request.dataset_id)
    if not request.x_series or not request.y_series:
        raise HTTPException(status_code=422, detail="x_series and y_series are required for a linear fit.")
    available = {parameter.series_identifier for parameter in dataset.parameters}
    if request.x_series not in available or request.y_series not in available:
        raise HTTPException(status_code=422, detail="x_series and y_series must name dataset series.")
    pairs = [(index, row.get(request.x_series), row.get(request.y_series)) for index, row in enumerate(dataset.si_values)]
    pairs = [(i, float(x), float(y)) for i, x, y in pairs if x is not None and y is not None and isfinite(x) and isfinite(y)]
    if len(pairs) < 3:
        raise HTTPException(status_code=422, detail="At least three complete finite point pairs are required.")
    xs, ys = [p[1] for p in pairs], [p[2] for p in pairs]
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
    result: dict[str, object] = {"id": run_id, "run_id": run_id, "dataset_id": request.dataset_id,
        "model": "linear", "equation": f"{request.y_series} = {slope} * {request.x_series} + {intercept}",
        "x_series": request.x_series, "y_series": request.y_series,
        "x_unit": x_parameter.si_unit or x_parameter.source_unit, "y_unit": y_parameter.si_unit or y_parameter.source_unit,
        "sample_count": len(pairs), "slope": slope, "intercept": intercept,
        "residual_standard_error": residual_standard_error, "uncertainty_95_approx": 1.96 * residual_standard_error,
        "points": [{"row_index": pair[0], "x": pair[1], "observed": pair[2],
                    "predicted": slope * pair[1] + intercept, "residual": residual}
                   for pair, residual in zip(pairs, residuals)],
        "method": "ordinary least squares; uncertainty is 1.96 times residual standard error"}
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
    matches = [p for p in dataset.parameters if p.series_identifier.casefold() in normalized_question
        or (p.canonical_name and p.canonical_name.replace("_", " ") in normalized_question)
        or p.source_header.casefold() in normalized_question]
    if not matches:
        return {"answer": "I can't answer that from the supplied dataset evidence. Mention a series name or canonical parameter to request a grounded summary.",
                "grounded": False, "evidence": [], "available_series": [p.series_identifier for p in dataset.parameters]}
    evidence, summaries = [], []
    for parameter in matches:
        values = [(i, row.get(parameter.series_identifier)) for i, row in enumerate(dataset.si_values)
                  if row.get(parameter.series_identifier) is not None and isfinite(row[parameter.series_identifier])]
        if not values:
            continue
        numbers = [value for _, value in values]
        average = mean(numbers)
        evidence.append({"dataset_id": request.dataset_id, "series_identifier": parameter.series_identifier,
                         "unit": parameter.si_unit or parameter.source_unit, "row_indices": [i for i, _ in values],
                         "count": len(values), "minimum": min(numbers), "maximum": max(numbers), "mean": average})
        summaries.append(f"{parameter.series_identifier}: {len(values)} finite normalized values; minimum {min(numbers)}, maximum {max(numbers)}, mean {average} {parameter.si_unit or parameter.source_unit or '(unit unknown)' }.")
    if not evidence:
        return {"answer": "No finite normalized values are available for the requested series.", "grounded": False, "evidence": []}
    return {"answer": " ".join(summaries), "grounded": True, "evidence": evidence,
            "limitations": "This endpoint reports descriptive statistics only; it does not infer causation or generate literature-backed claims."}
