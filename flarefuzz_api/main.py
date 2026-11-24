from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from httpx import HTTPError
from pydantic import ValidationError

from flarefuzz_api.advanced_methods import router as advanced_methods_router
from flarefuzz_api.calculations import router as calculations_router
from flarefuzz_api.frontend import router as frontend_router
from flarefuzz_api.gatherer import GathererError, gather_csv_inventories
from flarefuzz_api.models import DatasetMetadata, GatheredInventory, NormalizedCSVResponse, Provenance
from flarefuzz_api.parsers import DEFAULT_PARSER_REGISTRY, ParserContext, UnsupportedFileTypeError
from flarefuzz_api.routes.datasets import router as datasets_router
from flarefuzz_api.routes.simulations import router as diameter_simulations_router
from flarefuzz_api.routes.transport import router as transport_router
from flarefuzz_api.simulations import router as spherical_simulations_router

app = FastAPI(title="NASA PSI Combustion CSV Normalizer", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://mistily.github.io"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(frontend_router)
app.include_router(calculations_router)
app.include_router(advanced_methods_router)
app.include_router(spherical_simulations_router)
app.include_router(datasets_router)
app.include_router(diameter_simulations_router)
app.include_router(transport_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/normalize", response_model=NormalizedCSVResponse)
async def normalize_upload(file: UploadFile = File(...), provenance: Provenance = Form(Provenance.UNKNOWN),
                          experiment: str | None = Form(None), simulation: str | None = Form(None),
                          header_rows: int | None = Form(None, ge=1)) -> NormalizedCSVResponse:
    try:
        metadata = DatasetMetadata(provenance=provenance, experiment=experiment, simulation=simulation, source_url=None)
        options = {"header_rows": header_rows} if header_rows is not None else {}
        return DEFAULT_PARSER_REGISTRY.parse(await file.read(), ParserContext(
            filename=file.filename, media_type=file.content_type, metadata=metadata, options=options))
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/search", response_model=list[GatheredInventory])
async def search_and_inventory(term: str = Query(..., min_length=1), source: str = Query("psi"),
                               type: str | None = Query(None), from_: int = Query(0, alias="from", ge=0),
                               size: int = Query(10, ge=1, le=100), sort: str | None = Query(None),
                               order: str | None = Query(None, pattern="^(asc|desc)$"),
                               max_resources: int = Query(10, ge=1, le=100)) -> list[GatheredInventory]:
    try:
        return await gather_csv_inventories(term=term, source=source, resource_type=type,
            from_=from_, size=size, sort=sort, order=order, max_resources=max_resources)
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"NASA PSI request failed: {exc}") from exc
    except GathererError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
