"""Auditable, SI-only dimensionless analysis calculations."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/calculations", tags=["calculations"])


class InputProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=300)
    reference: str | None = Field(default=None, max_length=2000)


class ScalarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(gt=0, allow_inf_nan=False)
    provenance: InputProvenance


class VelocityInput(ScalarInput):
    value: float = Field(ge=0, allow_inf_nan=False)


class DimensionlessAnalysisRequest(BaseModel):
    """Inputs are explicitly SI; their source lineage is required per quantity."""
    model_config = ConfigDict(extra="forbid")

    density_kg_m3: ScalarInput
    velocity_m_s: VelocityInput
    characteristic_length_m: ScalarInput
    dynamic_viscosity_pa_s: ScalarInput
    specific_heat_capacity_j_kg_k: ScalarInput
    thermal_conductivity_w_m_k: ScalarInput
    mass_diffusivity_m2_s: ScalarInput


FORMULAS = {
    "Re": {
        "name": "Reynolds number",
        "equation": "rho * u * L / mu",
        "inputs": ["density_kg_m3", "velocity_m_s", "characteristic_length_m", "dynamic_viscosity_pa_s"],
    },
    "Pr": {
        "name": "Prandtl number",
        "equation": "mu * cp / k",
        "inputs": ["dynamic_viscosity_pa_s", "specific_heat_capacity_j_kg_k", "thermal_conductivity_w_m_k"],
    },
    "Sc": {
        "name": "Schmidt number",
        "equation": "mu / (rho * D_AB)",
        "inputs": ["dynamic_viscosity_pa_s", "density_kg_m3", "mass_diffusivity_m2_s"],
    },
    "Le": {
        "name": "Lewis number",
        "equation": "alpha / D_AB = k / (rho * cp * D_AB)",
        "inputs": ["thermal_conductivity_w_m_k", "density_kg_m3", "specific_heat_capacity_j_kg_k", "mass_diffusivity_m2_s"],
    },
}


@router.get("/dimensionless/formulas")
def dimensionless_formulas() -> dict[str, object]:
    """Describe the available calculation stage and required SI quantities."""
    return {
        "stage": "dimensionless_analysis",
        "position": "after normalization and before simulation",
        "formulas": FORMULAS,
        "required_units": {
            "density_kg_m3": "kg/m^3",
            "velocity_m_s": "m/s",
            "characteristic_length_m": "m",
            "dynamic_viscosity_pa_s": "Pa*s",
            "specific_heat_capacity_j_kg_k": "J/(kg*K)",
            "thermal_conductivity_w_m_k": "W/(m*K)",
            "mass_diffusivity_m2_s": "m^2/s",
        },
    }


@router.post("/dimensionless")
def calculate_dimensionless(request: DimensionlessAnalysisRequest) -> dict[str, object]:
    """Calculate Re, Pr, Sc and Le with explicit assumptions and input lineage."""
    rho = request.density_kg_m3.value
    velocity = request.velocity_m_s.value
    length = request.characteristic_length_m.value
    mu = request.dynamic_viscosity_pa_s.value
    cp = request.specific_heat_capacity_j_kg_k.value
    conductivity = request.thermal_conductivity_w_m_k.value
    diffusivity = request.mass_diffusivity_m2_s.value

    values = {
        "Re": rho * velocity * length / mu,
        "Pr": mu * cp / conductivity,
        "Sc": mu / (rho * diffusivity),
        "Le": conductivity / (rho * cp * diffusivity),
    }
    if not all(isfinite(value) for value in values.values()):
        # Avoid returning non-JSON values if otherwise-valid extreme inputs overflow.
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Inputs produced a non-finite result.")

    inputs = request.model_dump(mode="json")
    results = {
        symbol: {
            "name": FORMULAS[symbol]["name"],
            "value": value,
            "unit": "1",
            "equation": FORMULAS[symbol]["equation"],
            "input_names": FORMULAS[symbol]["inputs"],
            "input_provenance": {name: inputs[name]["provenance"] for name in FORMULAS[symbol]["inputs"]},
        }
        for symbol, value in values.items()
    }
    return {
        "calculation_id": str(uuid4()),
        "stage": "dimensionless_analysis",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "results": results,
        "assumptions": [
            "All input values are positive (velocity may be zero) and expressed in the SI units declared by their field names.",
            "The supplied density, velocity, characteristic length, and transport properties represent the same flow state and reference condition.",
            "A single characteristic velocity and length are appropriate for the Reynolds number; the caller selects their physical definitions.",
            "The fluid is treated as a continuum with Newtonian dynamic viscosity.",
            "The supplied mass diffusivity is the relevant binary or effective species diffusivity for Schmidt and Lewis numbers.",
            "These dimensionless groups are diagnostics, not a validated combustion model or simulation result.",
        ],
        "provenance": {
            "kind": "deterministic_formula_calculation",
            "formula_set": "dimensionless-transport-v1",
            "input_sources": {name: item["provenance"] for name, item in inputs.items()},
        },
    }
