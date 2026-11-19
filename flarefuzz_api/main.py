from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from httpx import HTTPError
from pydantic import ValidationError

from flarefuzz_api.calculations import router as calculations_router
from flarefuzz_api.frontend import router as frontend_router
from flarefuzz_api.gatherer import GathererError, gather_csv_inventories
from flarefuzz_api.models import (
    DatasetMetadata,
    GatheredInventory,
    NormalizedCSVResponse,
    Provenance,
)
from flarefuzz_api.parsers import (
    DEFAULT_PARSER_REGISTRY,
    ParserContext,
    UnsupportedFileTypeError,
)

app = FastAPI(title="NASA PSI Combustion CSV Normalizer", version="0.3.0")
app.include_router(frontend_router)
app.include_router(calculations_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/normalize", response_model=NormalizedCSVResponse)
async def normalize_upload(
    file: UploadFile = File(...),
    provenance: Provenance = Form(Provenance.UNKNOWN),
    experiment: str | None = Form(None),
    simulation: str | None = Form(None),
    header_rows: int | None = Form(None, ge=1),
) -> NormalizedCSVResponse:
    try:
        metadata = DatasetMetadata(
            provenance=provenance,
            experiment=experiment,
            simulation=simulation,
            source_url=None,
        )
        options = {"header_rows": header_rows} if header_rows is not None else {}
        return DEFAULT_PARSER_REGISTRY.parse(
            await file.read(),
            ParserContext(
                filename=file.filename,
                media_type=file.content_type,
                metadata=metadata,
                options=options,
            ),
        )
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/search", response_model=list[GatheredInventory])
async def search_and_inventory(
    term: str = Query(..., min_length=1),
    source: str = Query("psi"),
    type: str | None = Query(None),
    from_: int = Query(0, alias="from", ge=0),
    size: int = Query(10, ge=1, le=100),
    sort: str | None = Query(None),
    order: str | None = Query(None, pattern="^(asc|desc)$"),
    max_resources: int = Query(10, ge=1, le=100),
) -> list[GatheredInventory]:
    try:
        return await gather_csv_inventories(
            term=term,
            source=source,
            resource_type=type,
            from_=from_,
            size=size,
            sort=sort,
            order=order,
            max_resources=max_resources,
        )
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc
    except GathererError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
