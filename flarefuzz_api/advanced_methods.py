"""Auditable starter methods for kinetics, extinction screening, and inference.

These endpoints implement deliberately small, explicit models. They are not a
substitute for a validated multi-species reacting-flow PDE solver.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import exp, isfinite
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from scipy.integrate import solve_ivp

from flarefuzz_api.calculations import InputProvenance

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
R_UNIVERSAL_J_MOL_K = 8.31446261815324


class ProvenancedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(allow_inf_nan=False)
    unit: str
    provenance: InputProvenance


class KineticsRequest(BaseModel):
    """Isothermal irreversible first-order reaction, A -> products."""
    model_config = ConfigDict(extra="forbid")

    pre_exponential_factor_s_1: ProvenancedValue
    activation_energy_j_mol: ProvenancedValue
    temperature_k: ProvenancedValue
    initial_concentration_mol_m3: ProvenancedValue
    duration_s: float = Field(gt=0, allow_inf_nan=False)
    sample_count: int = Field(default=101, ge=2, le=2000)
    solver: str = Field(default="BDF", pattern="^(BDF|Radau)$")

    def validate_physical_units(self) -> None:
        expected = {
            "pre_exponential_factor_s_1": ("s^-1", "1/s"),
            "activation_energy_j_mol": ("J/mol",),
            "temperature_k": ("K",),
            "initial_concentration_mol_m3": ("mol/m^3",),
        }
        for field, units in expected.items():
            item = getattr(self, field)
            if item.unit not in units:
                raise ValueError(f"{field} unit must be one of {', '.join(units)}")
            if item.value <= 0:
                raise ValueError(f"{field} must be positive")


@router.post("/kinetics/first-order")
def integrate_first_order_kinetics(request: KineticsRequest) -> dict[str, object]:
    """Integrate a single isothermal first-order reaction using BDF or Radau."""
    try:
        request.validate_physical_units()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    A = request.pre_exponential_factor_s_1.value
    activation_energy = request.activation_energy_j_mol.value
    temperature = request.temperature_k.value
    concentration0 = request.initial_concentration_mol_m3.value
    try:
        rate_constant = A * exp(-activation_energy / (R_UNIVERSAL_J_MOL_K * temperature))
    except OverflowError as exc:
        raise HTTPException(status_code=422, detail="Arrhenius rate constant overflowed.") from exc
    if not isfinite(rate_constant):
        raise HTTPException(status_code=422, detail="Arrhenius rate constant is non-finite.")

    times = np.linspace(0.0, request.duration_s, request.sample_count)
    solution = solve_ivp(
        fun=lambda _time, state: [-rate_constant * state[0]],
        t_span=(0.0, request.duration_s),
        y0=[concentration0],
        method=request.solver,
        t_eval=times,
        rtol=1e-8,
        atol=max(concentration0 * 1e-12, 1e-15),
    )
    if not solution.success or solution.y.shape[1] != len(times):
        raise HTTPException(status_code=422, detail=f"Kinetics integration failed: {solution.message}")

    return {
        "calculation_id": str(uuid4()),
        "model": "isothermal irreversible first-order A-to-products reaction",
        "status": "illustrative_single_reaction_not_validated_mechanism",
        "solver": request.solver,
        "rate_constant_s_1": rate_constant,
        "formula": "k(T) = A * exp(-Ea / (R*T)); dC_A/dt = -k*C_A",
        "universal_gas_constant_j_mol_k": R_UNIVERSAL_J_MOL_K,
        "inputs": request.model_dump(mode="json"),
        "provenance": {
            name: getattr(request, name).provenance.model_dump()
            for name in (
                "pre_exponential_factor_s_1",
                "activation_energy_j_mol",
                "temperature_k",
                "initial_concentration_mol_m3",
            )
        },
        "points": [
            {"elapsed_s": float(t), "concentration_mol_m3": max(0.0, float(c))}
            for t, c in zip(solution.t, solution.y[0])
        ],
        "assumptions": [
            "Single irreversible first-order reaction; no intermediate species or reverse reaction.",
            "Temperature is constant and externally specified; no energy equation or heat release coupling is solved.",
            "A and Ea are supplied parameters with declared units and provenance, not inferred from fuel identity.",
            "BDF/Radau solve this small ODE only; this endpoint is not a detailed chemical mechanism or reacting-flow PDE solver.",
        ],
    }


class ExtinctionScreenRequest(BaseModel):
    """Compare a characteristic strain estimate to an experimental limit."""
    model_config = ConfigDict(extra="forbid")

    velocity_m_s: ProvenancedValue
    characteristic_length_m: ProvenancedValue
    extinction_limit_s_1: ProvenancedValue | None = None


@router.post("/extinction/strain-screen")
def extinction_strain_screen(request: ExtinctionScreenRequest) -> dict[str, object]:
    if request.velocity_m_s.unit != "m/s" or request.velocity_m_s.value < 0:
        raise HTTPException(status_code=422, detail="velocity_m_s must be nonnegative and expressed in m/s")
    if request.characteristic_length_m.unit != "m" or request.characteristic_length_m.value <= 0:
        raise HTTPException(status_code=422, detail="characteristic_length_m must be positive and expressed in m")
    strain = request.velocity_m_s.value / request.characteristic_length_m.value
    result: dict[str, object] = {
        "calculation_id": str(uuid4()),
        "model": "characteristic strain-rate screening",
        "strain_rate_s_1": strain,
        "formula": "a_est = U / L",
        "status": "insufficient_evidence_no_extinction_limit" if request.extinction_limit_s_1 is None else "screening_only",
        "provenance": {
            "velocity_m_s": request.velocity_m_s.provenance.model_dump(),
            "characteristic_length_m": request.characteristic_length_m.provenance.model_dump(),
        },
        "assumptions": [
            "U/L is a chosen characteristic strain-rate estimate, not the local flame strain field.",
            "Extinction limits depend on flame configuration, mixture, pressure, temperature, and the convention used to define strain.",
            "This comparison is a screening indicator, not an extinction-time prediction or safety decision.",
        ],
    }
    limit = request.extinction_limit_s_1
    if limit is not None:
        if limit.unit != "1/s" or limit.value <= 0:
            raise HTTPException(status_code=422, detail="extinction_limit_s_1 must be positive and expressed in 1/s")
        result["extinction_limit_s_1"] = limit.value
        result["limit_provenance"] = limit.provenance.model_dump()
        result["screening_comparison"] = "at_or_above_supplied_limit" if strain >= limit.value else "below_supplied_limit"
    return result


class LinearBayesianRequest(BaseModel):
    """Conjugate normal posterior for y = intercept + slope*x + Gaussian noise."""
    model_config = ConfigDict(extra="forbid")

    x: list[float] = Field(min_length=3, max_length=10000)
    y: list[float] = Field(min_length=3, max_length=10000)
    prior_mean: list[float] = Field(min_length=2, max_length=2)
    prior_covariance: list[list[float]] = Field(min_length=2, max_length=2)
    noise_std: float = Field(gt=0, allow_inf_nan=False)
    data_provenance: InputProvenance
    prior_provenance: InputProvenance


@router.post("/inference/linear-gaussian")
def linear_gaussian_posterior(request: LinearBayesianRequest) -> dict[str, object]:
    """Calculate an exact Gaussian posterior for intercept and slope."""
    x = np.asarray(request.x, dtype=float)
    y = np.asarray(request.y, dtype=float)
    prior_mean = np.asarray(request.prior_mean, dtype=float)
    prior_covariance = np.asarray(request.prior_covariance, dtype=float)
    if x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise HTTPException(status_code=422, detail="x and y must be paired finite arrays of equal length")
    if not np.isfinite(prior_mean).all() or not np.isfinite(prior_covariance).all():
        raise HTTPException(status_code=422, detail="Prior parameters must be finite")
    if not np.allclose(prior_covariance, prior_covariance.T):
        raise HTTPException(status_code=422, detail="prior_covariance must be symmetric")
    try:
        np.linalg.cholesky(prior_covariance)
        design = np.column_stack((np.ones_like(x), x))
        prior_precision = np.linalg.inv(prior_covariance)
        noise_variance = request.noise_std**2
        posterior_covariance = np.linalg.inv(prior_precision + design.T @ design / noise_variance)
        posterior_mean = posterior_covariance @ (
            prior_precision @ prior_mean + design.T @ y / noise_variance
        )
    except np.linalg.LinAlgError as exc:
        raise HTTPException(status_code=422, detail="Prior covariance must be positive definite") from exc
    if not np.isfinite(posterior_mean).all() or not np.isfinite(posterior_covariance).all():
        raise HTTPException(status_code=422, detail="Posterior calculation was numerically non-finite")

    return {
        "calculation_id": str(uuid4()),
        "model": "Bayesian linear regression with known Gaussian observation noise",
        "posterior_family": "multivariate_normal",
        "parameter_order": ["intercept", "slope"],
        "posterior_mean": posterior_mean.tolist(),
        "posterior_covariance": posterior_covariance.tolist(),
        "formula": "Sigma_n=(Sigma_0^-1 + X^T X/sigma^2)^-1; mu_n=Sigma_n(Sigma_0^-1 mu_0 + X^T y/sigma^2)",
        "sample_count": len(x),
        "data_provenance": request.data_provenance.model_dump(),
        "prior_provenance": request.prior_provenance.model_dump(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "assumptions": [
            "Observations follow y = intercept + slope*x + independent Gaussian noise with known standard deviation.",
            "The prior is a proper bivariate Gaussian and input values are already in consistent units.",
            "The posterior quantifies this regression model only; it is not automatically a combustion-physics posterior.",
        ],
    }


@router.get("/methods")
def method_catalog() -> dict[str, object]:
    """Expose current calculation methods and research-only work explicitly."""
    return {
        "methods": [
            {"name": "Thermal transport", "status": "available", "endpoint": "POST /api/calculations/dimensionless", "formula": "Pr = mu*cp/k"},
            {"name": "Species transport", "status": "available", "endpoint": "POST /api/calculations/dimensionless", "formula": "Le_i = alpha/D_i; Sc_i = nu/D_i"},
            {"name": "Chemical kinetics", "status": "limited_single_step", "endpoint": "POST /api/analysis/kinetics/first-order", "formula": "k(T)=A*exp(-Ea/(R*T))"},
            {"name": "Flame extinction", "status": "screening_only", "endpoint": "POST /api/analysis/extinction/strain-screen", "formula": "a_est=U/L; compare only with a configuration-specific supplied limit"},
            {"name": "Bayesian inference", "status": "available_for_linear_gaussian_model", "endpoint": "POST /api/analysis/inference/linear-gaussian", "formula": "p(theta|D) proportional to p(D|theta)*p(theta)"},
            {"name": "Stiff numerical integration", "status": "available_for_single_reaction_ode", "solvers": ["BDF", "Radau"]},
            {"name": "Neural operators", "status": "research_not_implemented", "note": "No training data, trained operator, validation benchmark, or inference endpoint is currently provided."},
        ],
        "scope_note": "Available methods are bounded calculation components, not an integrated or validated reactive-flow PDE solver.",
    }
