"""Optional Cantera properties attached to an existing model run."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from flarefuzz_api.frontend import _RUNS
from flarefuzz_api.routes.simulations import DIAMETER_RUNS
from flarefuzz_api.simulation.cantera_properties import CanteraUnavailableError, gas_properties

router = APIRouter(prefix="/api/simulations", tags=["simulations"])


class TransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mechanism: Literal["gri30.yaml", "h2o2.yaml"]
    temperature_k: float = Field(gt=0, allow_inf_nan=False)
    pressure_pa: float = Field(gt=0, allow_inf_nan=False)
    composition: dict[str, float]
    composition_provenance: str = Field(min_length=1, max_length=300)


@router.post("/{run_id}/transport")
def calculate_run_transport(run_id: str, request: TransportRequest) -> dict[str, object]:
    if run_id not in _RUNS and run_id not in DIAMETER_RUNS:
        raise HTTPException(status_code=404, detail="Simulation run not found.")
    try:
        properties = gas_properties(mechanism=request.mechanism,
            temperature_k=request.temperature_k, pressure_pa=request.pressure_pa,
            composition=request.composition)
    except CanteraUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "run_id": run_id,
        "status": "thermophysical_properties_calculated",
        "data_kind": "derived_transport_properties",
        "properties": properties,
        "provenance": {
            "mechanism": request.mechanism,
            "state": {"temperature_k": request.temperature_k, "pressure_pa": request.pressure_pa,
                      "composition_mole_fractions": request.composition},
            "composition_source": request.composition_provenance,
        },
        "limitations": ["Property calculation only; these properties are not retroactively coupled to the fitted diameter model.",
                        "No flame simulation or transport-property time history is calculated by this endpoint."],
    }
