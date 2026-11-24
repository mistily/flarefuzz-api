"""Diameter CSV upload and NASA PSI allowlisted download routes."""
from __future__ import annotations

from uuid import uuid4
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from flarefuzz_api.datasets.diameter_parser import DiameterCSVError, parse_diameter_csv
from flarefuzz_api.datasets.psi_download import download_psi_csv
from flarefuzz_api.datasets.quality import classify_diameter_dataset
from flarefuzz_api.gatherer import _get_investigation, _study_version, _inventory, _walk_files, _download_csv, GathererError

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



@router.get("/psi/PSI-117/diameter-candidates")
async def inspect_psi_117_diameter_candidates() -> dict[str, object]:
    """Inspect NASA PSI-117 CSV files without inventing measurements or ingesting unrelated data."""
    candidates: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False) as client:
        try:
            metadata = await _get_investigation(client, "PSI-117")
            version = _study_version(metadata)
            if version is None:
                raise HTTPException(status_code=422, detail="NASA metadata did not supply a PSI-117 study version.")
            inventory = await _inventory(client, "PSI-117", version)
        except (httpx.HTTPError, GathererError) as exc:
            raise HTTPException(status_code=502, detail=f"NASA PSI-117 inventory unavailable: {exc}") from exc

        files = [file for file in _walk_files(inventory)
                 if PurePosixPath(str(file["key"])).suffix.lower() == ".csv"]
        # Bound work per request to avoid an expensive public endpoint.
        for file in files[:30]:
            item: dict[str, object] = {
                "filename": PurePosixPath(str(file["key"])).name,
                "file_id": str(file["id"]),
                "status": "unverified",
            }
            if file.get("restricted") or file.get("restrictedAccess") or file.get("visible") is False:
                item["status"] = "restricted"
            else:
                try:
                    source_url, content = await _download_csv(client, "PSI-117", version, file)
                    item["source_url"] = source_url
                    # Return actual headers even for files that are not diameter series.
                    import csv
                    import io
                    item["columns"] = next(csv.reader(io.StringIO(content.decode("utf-8-sig"))), [])
                    parsed = parse_diameter_csv(content)
                    item["status"] = "valid_diameter_series"
                    item["experiment_ids"] = sorted(parsed["runs"])
                    item["row_count"] = sum(len(run["time_s"]) for run in parsed["runs"].values())
                    item["issues"] = parsed["issues"][:10]
                except DiameterCSVError as exc:
                    item["status"] = "not_compatible"
                    item["reason"] = exc.code
                except (httpx.HTTPError, GathererError, UnicodeDecodeError, csv.Error, ValueError) as exc:
                    item["status"] = "download_or_parse_error"
                    item["reason"] = type(exc).__name__
            candidates.append(item)

    return {
        "study_id": "PSI-117",
        "study_version": version,
        "csv_file_count": len(files),
        "inspected_count": len(candidates),
        "truncated": len(files) > len(candidates),
        "candidates": candidates,
        "note": "Only validated measured series qualify; no synthetic data or automatic fitting.",
    }
