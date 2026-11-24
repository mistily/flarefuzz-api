"""Optional Cantera thermophysical-property adapter with allowlisted mechanisms."""
from __future__ import annotations

from typing import Any

SUPPORTED_MECHANISMS = {"gri30.yaml", "h2o2.yaml"}


class CanteraUnavailableError(RuntimeError):
    pass


def gas_properties(*, mechanism: str, temperature_k: float, pressure_pa: float,
                   composition: dict[str, float]) -> dict[str, Any]:
    if mechanism not in SUPPORTED_MECHANISMS:
        raise ValueError(f"Unsupported mechanism; choose one of {sorted(SUPPORTED_MECHANISMS)}")
    if temperature_k <= 0 or pressure_pa <= 0:
        raise ValueError("Temperature and pressure must be positive SI values.")
    if not composition or any(value < 0 for value in composition.values()) or abs(sum(composition.values()) - 1.0) > 1e-6:
        raise ValueError("Composition must be nonnegative mole fractions summing to one.")
    try:
        import cantera as ct
    except ImportError as exc:
        raise CanteraUnavailableError("Cantera is optional and is not installed. Install the tested Cantera build to enable this adapter.") from exc
    gas = ct.Solution(mechanism)
    unknown = set(composition) - set(gas.species_names)
    if unknown:
        raise ValueError(f"Species are not present in the selected mechanism: {sorted(unknown)}")
    gas.TPX = temperature_k, pressure_pa, composition
    thermal_diffusivity = gas.thermal_conductivity / (gas.density * gas.cp_mass)
    return {
        "mechanism": mechanism,
        "cantera_version": ct.__version__,
        "temperature_k": float(gas.T),
        "pressure_pa": float(gas.P),
        "density_kg_m3": float(gas.density),
        "cp_j_kg_k": float(gas.cp_mass),
        "viscosity_pa_s": float(gas.viscosity),
        "thermal_conductivity_w_m_k": float(gas.thermal_conductivity),
        "thermal_diffusivity_m2_s": float(thermal_diffusivity),
        "mass_diffusivity_m2_s": [float(value) for value in gas.mix_diff_coeffs_mass],
        "species": list(gas.species_names),
        "note": "Properties are evaluated for the selected gas mechanism and state; this is not a flame simulation.",
    }
