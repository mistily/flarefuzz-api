"""Conservative, auditable first-cut controlled droplet-flame envelope model."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite, pi
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter(prefix="/api/simulations", tags=["simulations"])


class InputProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=300)
    reference: str | None = Field(default=None, max_length=2000)


class PositiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(gt=0, allow_inf_nan=False)
    provenance: InputProvenance


class NonNegativeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(ge=0, allow_inf_nan=False)
    provenance: InputProvenance


class OxygenFractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(gt=0, le=1, allow_inf_nan=False)
    provenance: InputProvenance


class SphericalDropletFlameRequest(BaseModel):
    """SI inputs for a fixed-condition, D-squared-law envelope projection."""
    model_config = ConfigDict(extra="forbid")

    detection_time: datetime
    initial_droplet_diameter_m: PositiveInput
    burning_constant_m2_s: PositiveInput
    flame_to_droplet_diameter_ratio: PositiveInput
    ambient_temperature_k: PositiveInput
    ambient_pressure_pa: PositiveInput
    oxygen_mole_fraction: OxygenFractionInput
    ambient_velocity_m_s: NonNegativeInput
    sample_count: int = Field(default=101, ge=2, le=500)

    @field_validator("detection_time")
    @classmethod
    def require_timezone_aware_detection_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("detection_time must include a timezone")
        return value

    @field_validator("flame_to_droplet_diameter_ratio")
    @classmethod
    def require_expanding_flame_envelope(cls, value: PositiveInput) -> PositiveInput:
        if value.value <= 1:
            raise ValueError("flame_to_droplet_diameter_ratio must be greater than 1")
        return value


@router.post("/spherical-droplet-flame")
def simulate_spherical_droplet_flame(
    request: SphericalDropletFlameRequest,
) -> dict[str, object]:
    """Return a fixed-environment spherical envelope through predicted burnout.

    Uses D_drop(t)^2 = D_detect^2 - K*t and a constant flame/drop diameter
    ratio. Ambient values are required context with provenance, not a chemistry
    or transport-property model and do not independently determine K or the ratio.
    """
    diameter0 = request.initial_droplet_diameter_m.value
    burning_constant = request.burning_constant_m2_s.value
    ratio = request.flame_to_droplet_diameter_ratio.value
    burnout_duration = diameter0 * diameter0 / burning_constant
    if not isfinite(burnout_duration) or burnout_duration <= 0:
        raise HTTPException(status_code=422, detail="Inputs produce an invalid burnout duration.")

    detection_time = request.detection_time.astimezone(timezone.utc)
    points: list[dict[str, object]] = []
    for index in range(request.sample_count):
        elapsed = burnout_duration * index / (request.sample_count - 1)
        remaining_diameter = diameter0 * (max(0.0, 1.0 - elapsed / burnout_duration) ** 0.5)
        flame_diameter = ratio * remaining_diameter
        flame_radius = flame_diameter / 2.0
        sphere_volume = (4.0 / 3.0) * pi * flame_radius**3
        sphere_surface_area = 4.0 * pi * flame_radius**2
        values = (elapsed, remaining_diameter, flame_diameter, sphere_volume, sphere_surface_area)
        if not all(isfinite(value) for value in values):
            raise HTTPException(status_code=422, detail="Inputs produce a non-finite envelope value.")
        points.append({
            "elapsed_s": elapsed,
            "time": (detection_time + timedelta(seconds=elapsed)).isoformat(),
            "droplet_diameter_m": remaining_diameter,
            "flame_envelope": {
                "geometry": "sphere",
                "diameter_m": flame_diameter,
                "radius_m": flame_radius,
                "volume_m3": sphere_volume,
                "surface_area_m2": sphere_surface_area,
            },
        })

    inputs = request.model_dump(mode="json")
    return {
        "simulation_id": str(uuid4()),
        "model": "fixed-environment spherical droplet flame envelope",
        "model_version": "0.1.0",
        "status": "illustrative_prediction_not_validated",
        "detection_time": detection_time.isoformat(),
        "predicted_droplet_burnout_time": (detection_time + timedelta(seconds=burnout_duration)).isoformat(),
        "predicted_remaining_burnout_duration_s": burnout_duration,
        "time_origin": "detection_time; this model does not reconstruct pre-detection history",
        "inputs": inputs,
        "environment": {
            "ambient_temperature_k": inputs["ambient_temperature_k"],
            "ambient_pressure_pa": inputs["ambient_pressure_pa"],
            "oxygen_mole_fraction": inputs["oxygen_mole_fraction"],
            "ambient_velocity_m_s": inputs["ambient_velocity_m_s"],
            "use": "fixed environmental context; these values are not used to derive the supplied burning constant or flame ratio",
        },
        "parameters": {
            "burning_constant_m2_s": burning_constant,
            "flame_to_droplet_diameter_ratio": ratio,
        },
        "formulae": {
            "droplet_diameter": "sqrt(max(0, D_detect^2 - K * elapsed_time))",
            "flame_diameter": "flame_to_droplet_diameter_ratio * droplet_diameter",
            "burnout_duration": "D_detect^2 / K",
        },
        "units": {"time": "s", "diameter": "m", "volume": "m^3", "surface_area": "m^2"},
        "assumptions": [
            "The droplet remains spherical and follows a constant-burning-constant D-squared law after detection.",
            "The flame envelope is spherical and its diameter-to-droplet-diameter ratio remains constant for the entire projection.",
            "Ambient temperature, pressure, oxygen fraction, and velocity remain fixed at the supplied values.",
            "The supplied burning constant and flame ratio are independently measured or explicitly hypothesized; this endpoint does not infer them from the environment.",
            "Predicted droplet burnout is only an endpoint proxy. It is not a validated prediction of flame extinction, ignition time, or real fire behavior.",
        ],
        "provenance": {
            "kind": "deterministic_illustrative_simulation",
            "input_sources": {
                name: item["provenance"]
                for name, item in inputs.items()
                if isinstance(item, dict) and "provenance" in item
            },
        },
        "points": points,
    }
