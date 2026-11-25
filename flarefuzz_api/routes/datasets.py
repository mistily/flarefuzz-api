"""Diameter datasets from uploaded files or NASA PSI signed-resource URLs."""
from __future__ import annotations

import csv
import io
from uuid import uuid4
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from flarefuzz_api.datasets.diameter_parser import DiameterCSVError, parse_diameter_resource
from flarefuzz_api.datasets.psi_download import download_psi_resources
from flarefuzz_api.datasets.quality import classify_diameter_dataset
from flarefuzz_api.gatherer import (
    GathererError,
    _download_csv,
    _get_investigation,
    _inventory,
    _study_version,
    _walk_files,
)

router = APIRouter(prefix="/api/datasets", tags=["datasets"])
DIAMETER_DATASETS: dict[str, dict[str, object]] = {}


def _parse_resources(resources: list[dict[str, object]]) -> tuple[dict[str, object], list[dict[str, str]]]:
    parsed_resources: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for resource in resources:
        kind = str(resource["format"])
        if kind not in {"csv", "tsv"}:
            errors.append({"resource": str(resource["filename"]), "code": "unsupported_resource_format",
                           "message": f"{kind} resource is not a supported diameter data table."})
            continue
        try:
            parsed_resources.append({"filename": resource["filename"], "url": resource["url"],
                                     "parsed": parse_diameter_resource(resource["content"], resource_format=kind)})
        except DiameterCSVError as exc:
            errors.append({"resource": str(resource["filename"]), "code": exc.code, "message": str(exc)})
    categories = {entry["parsed"]["dataset_category"] for entry in parsed_resources}
    if len(categories) > 1:
        raise DiameterCSVError("mixed_dataset_categories", "Resolved resources contain different dataset categories; ingest them separately.")
    if not parsed_resources:
        raise DiameterCSVError(errors[0]["code"] if errors else "unsupported_dataset_category",
                               errors[0]["message"] if errors else "No supported diameter resources found.")
    category = next(iter(categories))
    if category == "diameter_time_series":
        combined_runs: dict[str, object] = {}
        issues: list[dict[str, object]] = []
        for entry in parsed_resources:
            for run_id, run in entry["parsed"]["runs"].items():
                unique_id = run_id if run_id not in combined_runs else f"{entry['filename']}::{run_id}"
                combined_runs[unique_id] = run
            issues.extend(entry["parsed"].get("issues", []))
        parsed = {"dataset_category": category, "runs": combined_runs, "issues": issues,
                  "time_unit": "s", "diameter_unit": "m"}
    else:
        comparisons = [item for entry in parsed_resources for item in entry["parsed"]["comparisons"]]
        errors_m = [float(item["error_m"]) for item in comparisons]
        parsed = {"dataset_category": category, "comparisons": comparisons,
                  "metrics": {"sample_count": len(comparisons),
                              "bias_m": sum(errors_m) / len(errors_m),
                              "mae_m": sum(abs(value) for value in errors_m) / len(errors_m),
                              "rmse_m": (sum(value * value for value in errors_m) / len(errors_m)) ** 0.5},
                  "issues": [issue for entry in parsed_resources for issue in entry["parsed"].get("issues", [])]}
    parsed["resources"] = [{"filename": item["filename"], "url": item["url"], "format": item["parsed"]["dataset_category"]}
                            for item in parsed_resources]
    return parsed, errors


def _store_dataset(*, filename: str, source_url: str | None, nasa_accession: str | None,
                   resources: list[dict[str, object]]) -> dict[str, object]:
    parsed, resource_issues = _parse_resources(resources)
    dataset_id = str(uuid4())
    record: dict[str, object] = {"id": dataset_id, "filename": filename, "source_url": source_url,
                                "nasa_accession": nasa_accession, "parsed": parsed,
                                "dataset_category": parsed["dataset_category"],
                                "quality": classify_diameter_dataset(parsed), "resource_issues": resource_issues}
    DIAMETER_DATASETS[dataset_id] = record
    if parsed["dataset_category"] == "diameter_time_series":
        row_count = sum(len(run["time_s"]) for run in parsed["runs"].values())
        experiment_ids = sorted(parsed["runs"])
    else:
        row_count = len(parsed["comparisons"])
        experiment_ids = sorted({item["experiment_id"] for item in parsed["comparisons"] if item["experiment_id"]})
    return {"id": dataset_id, "filename": filename, "source_url": source_url,
            "nasa_accession": nasa_accession, "dataset_category": parsed["dataset_category"],
            "quality": record["quality"], "experiment_ids": experiment_ids,
            "row_count": row_count, "issues": parsed.get("issues", []), "resource_issues": resource_issues,
            "resources": parsed.get("resources", [])}


@router.post("/diameter/ingest", status_code=201)
async def ingest_diameter_csv(file: UploadFile = File(...), source_url: str | None = Query(default=None, max_length=2000),
                              nasa_accession: str | None = Query(default=None, max_length=100)) -> dict[str, object]:
    if not (file.filename or "").lower().endswith((".csv", ".tsv")):
        raise HTTPException(status_code=415, detail="Only CSV and TSV files are supported.")
    content = await file.read(10_000_001)
    if len(content) > 10_000_000:
        raise HTTPException(status_code=413, detail="Resource exceeds the 10 MB limit.")
    fmt = "tsv" if file.filename.lower().endswith(".tsv") else "csv"
    try:
        return _store_dataset(filename=file.filename or "upload", source_url=source_url, nasa_accession=nasa_accession,
                              resources=[{"filename": file.filename or "upload", "url": source_url or "upload", "format": fmt, "content": content}])
    except DiameterCSVError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/psi/diameter/ingest", status_code=201)
async def ingest_nasa_psi_diameter_resource(url: str = Query(..., min_length=1, max_length=4000),
                                            nasa_accession: str = Query(..., min_length=1, max_length=100)) -> dict[str, object]:
    """Resolve signed NASA/S3 URLs or JSON manifests, fetch resources, then inspect data columns."""
    try:
        resources = await download_psi_resources(url)
        return _store_dataset(filename=str(resources[0]["filename"]), source_url=url,
                              nasa_accession=nasa_accession, resources=resources)
    except DiameterCSVError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "resource_resolution_failed", "message": str(exc)}) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="NASA PSI resource fetch failed.") from exc


@router.get("/diameter/{dataset_id}/quality")
def diameter_dataset_quality(dataset_id: str) -> dict[str, object]:
    record = DIAMETER_DATASETS.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Diameter dataset not found.")
    return {"dataset_id": dataset_id, "dataset_category": record["dataset_category"],
            "quality": record["quality"], "issues": record["parsed"].get("issues", []),
            "resource_issues": record["resource_issues"]}


@router.get("/diameter/{dataset_id}/series")
def diameter_dataset_series(dataset_id: str) -> dict[str, object]:
    record = DIAMETER_DATASETS.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Diameter dataset not found.")
    parsed = record["parsed"]
    if record["dataset_category"] == "diameter_time_series":
        values: object = parsed["runs"]
    else:
        values = parsed["comparisons"]
    return {"dataset_id": dataset_id, "dataset_category": record["dataset_category"],
            "units": {"time": "s", "diameter": "m"}, "series": values,
            "metrics": parsed.get("metrics")}


@router.get("/diameter/{dataset_id}")
def get_diameter_dataset(dataset_id: str) -> dict[str, object]:
    record = DIAMETER_DATASETS.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Diameter dataset not found.")
    return {"id": record["id"], "filename": record["filename"], "source_url": record["source_url"],
            "nasa_accession": record["nasa_accession"], "dataset_category": record["dataset_category"],
            "quality": record["quality"], "data": record["parsed"], "resource_issues": record["resource_issues"]}


@router.get("/psi/PSI-117/diameter-candidates")
async def inspect_psi_117_diameter_candidates() -> dict[str, object]:
    """Inspect NASA PSI-117 CSV files without ingesting unrelated data."""
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
                    item["columns"] = next(csv.reader(io.StringIO(content.decode("utf-8-sig"))), [])
                    parsed = parse_diameter_resource(content, resource_format="csv")
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
