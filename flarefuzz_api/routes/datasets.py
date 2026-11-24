"""Diameter CSV upload and NASA PSI allowlisted download routes."""
from __future__ import annotations

from uuid import uuid4

import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from flarefuzz_api.datasets.diameter_parser import DiameterCSVError, parse_diameter_csv
from flarefuzz_api.datasets.psi_download import download_psi_csv
from flarefuzz_api.datasets.quality import classify_diameter_dataset

router = APIRouter(prefix="/api/datasets", tags=["datasets"])
DIAMETER_DATASETS: dict[str, dict[str, object]] = {}


@router.post("/diameter/ingest", status_code=201)
async def ingest_diameter_csv(file: UploadFile = File(...), source_url: str | None = Query(default=None, max_length=2000), nasa_accession: str | None = Query(default=None, max_length=100)) -> dict[str, object]:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Only CSV files are supported.")
    content = await file.read(10_000_001)
    if len(content) > 10_000_000:
        raise HTTPException(status_code=413, detail="CSV exceeds the 10 MB upload limit.")
    try:
        parsed = parse_diameter_csv(content)
    except DiameterCSVError as exc:
        status = 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    dataset_id = str(uuid4())
    record = {
        "id": dataset_id,
        "filename": file.filename,
        "source_url": source_url,
        "nasa_accession": nasa_accession,
        "parsed": parsed,
        "quality": classify_diameter_dataset(parsed),
    }
    DIAMETER_DATASETS[dataset_id] = record
    return {key: record[key] for key in ("id", "filename", "source_url", "nasa_accession", "quality")} | {
        "experiment_ids": sorted(parsed["runs"]),
        "row_count": sum(len(run["time_s"]) for run in parsed["runs"].values()),
        "issues": parsed["issues"],
    }


@router.post("/psi/diameter/ingest", status_code=201)
async def ingest_nasa_psi_diameter_csv(url: str = Query(..., min_length=1, max_length=2000), nasa_accession: str = Query(..., min_length=1, max_length=100)) -> dict[str, object]:
    try:
        content = await download_psi_csv(url)
        parsed = parse_diameter_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="NASA PSI CSV download failed.") from exc
    dataset_id = str(uuid4())
    record = {"id": dataset_id, "filename": url.rsplit("/", 1)[-1], "source_url": url,
              "nasa_accession": nasa_accession, "parsed": parsed,
              "quality": classify_diameter_dataset(parsed)}
    DIAMETER_DATASETS[dataset_id] = record
    return {"id": dataset_id, "source_url": url, "nasa_accession": nasa_accession,
            "experiment_ids": sorted(parsed["runs"]), "row_count": sum(len(run["time_s"]) for run in parsed["runs"].values()),
            "quality": record["quality"], "issues": parsed["issues"]}


@router.get("/diameter/{dataset_id}")
def get_diameter_dataset(dataset_id: str) -> dict[str, object]:
    record = DIAMETER_DATASETS.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Diameter dataset not found.")
    return {"id": record["id"], "filename": record["filename"], "source_url": record["source_url"],
            "nasa_accession": record["nasa_accession"], "quality": record["quality"],
            "runs": record["parsed"]["runs"], "issues": record["parsed"]["issues"]}
