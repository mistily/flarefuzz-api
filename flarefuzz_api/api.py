"""Mountable FastAPI router; no database required for a single-process demo."""
from __future__ import annotations
import os
import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from .ingestion import ingest_csv
from .models import DatasetResult, FitRequest, SchemaSuggestion, ValidationStatus
from .science import BayesianD2Inference, TraitGraph
from .ai import suggest_headers

router = APIRouter(prefix="/api/science", tags=["science"])
# DEMO ONLY: per-process in-memory storage, not durable and not shared across workers.
DATASETS: dict[str, DatasetResult] = {}
MAX_DATASETS = 20

@router.post("/datasets/ingest", response_model=DatasetResult)
async def ingest(file: UploadFile = File(...), header_rows: int | None = Query(default=None, ge=1, le=10)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(415, "Only CSV uploads supported")
    content = await file.read(10_000_001)
    try: dataset = ingest_csv(content, source_uri=os.path.basename(file.filename or "upload.csv"), header_rows=header_rows)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    if len(DATASETS) >= MAX_DATASETS and dataset.dataset_id not in DATASETS:
        DATASETS.pop(next(iter(DATASETS)))
    DATASETS[dataset.dataset_id] = dataset
    return dataset

@router.get("/datasets/{dataset_id}", response_model=DatasetResult)
def get_dataset(dataset_id: str):
    if dataset_id not in DATASETS: raise HTTPException(404, "Dataset not found")
    return DATASETS[dataset_id]

@router.get("/datasets/{dataset_id}/quality")
def quality(dataset_id: str):
    d = get_dataset(dataset_id)
    return {"dataset_id": dataset_id, "parameters": d.parameters, "issues": d.validation_issues}

@router.post("/models/fit")
def fit(req: FitRequest):
    d = get_dataset(req.dataset_id)
    params = {p.series_identifier: p for p in d.parameters}
    if req.time_series not in params or req.diameter_series not in params:
        raise HTTPException(422, "Unknown series identifier")
    if params[req.time_series].canonical_name != "time" or params[req.time_series].si_unit != "s":
        raise HTTPException(422, "Time series must be recognized and SI-normalized to seconds")
    if params[req.diameter_series].canonical_name != "droplet_diameter" or params[req.diameter_series].si_unit != "m":
        raise HTTPException(422, "Diameter series must be droplet diameter, SI-normalized to meters")
    if params[req.time_series].validation_status != ValidationStatus.VALIDATED or params[req.diameter_series].validation_status != ValidationStatus.VALIDATED:
        raise HTTPException(422, "Selected measurements require validation before fitting")
    pairs = [(row.get(req.time_series), row.get(req.diameter_series)) for row in d.si_values]
    valid = [(float(t), float(diameter)) for t, diameter in pairs if t is not None and diameter is not None]
    if len(valid) < 3: raise HTTPException(422, "Not enough valid measurement pairs")
    initial = req.initial_diameter_m
    if initial is None:
        # No inferred initial diameter: the first measurement is not necessarily t=0.
        raise HTTPException(422, "Provide independently established initial_diameter_m")
    try:
        return BayesianD2Inference().fit([x[0] for x in valid], [x[1] for x in valid],
            initial, req.prior_mean, req.prior_std, req.noise_std_m2, req.dataset_id)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc

class HeaderRequest(BaseModel):
    headers: list[str]

@router.post("/headers/suggest", response_model=list[SchemaSuggestion])
async def headers_suggest(request: HeaderRequest):
    try: return await suggest_headers(request.headers)
    except RuntimeError as exc: raise HTTPException(503, str(exc)) from exc
    except (ValueError, KeyError, httpx.HTTPError) as exc: raise HTTPException(502, "AI schema suggestion unavailable") from exc

@router.get("/capabilities")
def capabilities():
    graph = TraitGraph()
    graph.add("droplet_diameter_squared", ["droplet_diameter"])
    graph.add("burning_constant", ["time", "droplet_diameter", "initial_diameter"])
    return {"traits": {key: sorted(val) for key, val in graph.dependencies.items()},
            "models": ["D2-law Bayesian parameter estimation"],
            "persistence": "in-memory demo only", "ai": "optional HF schema suggestions"}
