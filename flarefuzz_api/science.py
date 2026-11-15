"""Deterministic computation and conjugate Bayesian inference for D-squared law."""
from __future__ import annotations
from abc import ABC, abstractmethod
from collections import defaultdict
import math
import numpy as np
from .models import ApplicabilityResult, InferenceResult

class PhysicalModel(ABC):
    version = "1.0"
    @abstractmethod
    def check_applicability(self, inputs: dict) -> ApplicabilityResult: ...
    @abstractmethod
    def compute(self, inputs: dict) -> dict: ...

class DropletD2Model(PhysicalModel):
    """Idealized D²(t)=D0²-Kt. Applicable only when regime is established."""
    def check_applicability(self, inputs: dict) -> ApplicabilityResult:
        reasons = []
        if not inputs.get("regime_verified", False):
            reasons.append("D-squared-law regime has not been independently verified")
        if inputs.get("initial_diameter_m", 0) <= 0:
            reasons.append("Positive initial diameter required")
        if inputs.get("burning_constant_m2_s", -1) < 0:
            reasons.append("Burning constant must be nonnegative")
        return ApplicabilityResult(applicable=not reasons, reasons=reasons)
    def compute(self, inputs: dict) -> dict:
        check = self.check_applicability(inputs)
        if not check.applicable: raise ValueError("; ".join(check.reasons))
        t = float(inputs["time_s"])
        if t < 0: raise ValueError("Time must be nonnegative")
        d2 = inputs["initial_diameter_m"]**2 - inputs["burning_constant_m2_s"] * t
        return {"diameter_squared_m2": max(0.0, d2), "model_version": self.version,
                "interpretation": "idealized model prediction"}

class TraitGraph:
    def __init__(self): self.dependencies: dict[str, set[str]] = {}
    def add(self, trait: str, depends_on: list[str]): self.dependencies[trait] = set(depends_on)
    def missing_inputs(self, trait: str, available: set[str]) -> list[str]:
        missing = set()
        def visit(node: str, stack: set[str]):
            if node in available: return
            if node in stack: raise ValueError("Trait dependency cycle detected")
            if node not in self.dependencies:
                missing.add(node); return
            for dep in self.dependencies[node]: visit(dep, stack | {node})
        visit(trait, set())
        return sorted(missing)
    def is_identifiable(self, trait: str, available: set[str]) -> bool:
        return not self.missing_inputs(trait, available)

class BayesianD2Inference:
    """Conjugate Gaussian posterior for K given known D0 and observation noise.

    y_i = D0² - D_i² = K*t_i + epsilon_i, epsilon~N(0,sigma²).
    Gaussian prior on K; posterior truncated to K>=0 by numerical quadrature.
    This is an illustrative scientific baseline, not a validated combustion model.
    """
    def fit(self, time_s: list[float], diameter_m: list[float],
            initial_diameter_m: float, prior_mean: float = 1e-7,
            prior_std: float = 1e-6, noise_std_m2: float = 1e-7,
            source_dataset_id: str | None = None) -> InferenceResult:
        t = np.asarray(time_s, dtype=float)
        d = np.asarray(diameter_m, dtype=float)
        if t.ndim != 1 or len(t) != len(d) or len(t) < 3:
            raise ValueError("At least three paired time/diameter measurements required")
        if not (np.isfinite(t).all() and np.isfinite(d).all()):
            raise ValueError("Measurements must be finite")
        if (t < 0).any() or (d <= 0).any() or initial_diameter_m <= 0:
            raise ValueError("Time must be >= 0 and diameters > 0")
        if len(np.unique(t)) < 2: raise ValueError("At least two distinct timestamps required")
        if prior_std <= 0 or noise_std_m2 <= 0 or prior_mean < 0:
            raise ValueError("Invalid prior or noise specification")
        y = initial_diameter_m**2 - d**2
        precision = 1 / prior_std**2 + float(np.dot(t,t)) / noise_std_m2**2
        var = 1 / precision
        mu = var * (prior_mean / prior_std**2 + float(np.dot(t,y)) / noise_std_m2**2)
        sd = math.sqrt(var)
        # Nonnegative K posterior: integrate a truncated normal density on a grid.
        upper = max(mu + 9*sd, 9*sd, 1e-15)
        grid = np.linspace(0.0, upper, 5001)
        weights = np.exp(-0.5 * ((grid-mu)/sd)**2)
        total = np.trapezoid(weights, grid)
        if total <= 0 or not math.isfinite(total):
            raise ValueError("Posterior numerically degenerate; check noise and prior")
        pdf = weights / total
        cdf = np.r_[0, np.cumsum((pdf[:-1]+pdf[1:])*0.5*np.diff(grid))]
        cdf /= cdf[-1]
        mean = float(np.trapezoid(grid*pdf, grid))
        low, high = [float(np.interp(q, cdf, grid)) for q in (0.025, 0.975)]
        residuals = y - mean*t
        return InferenceResult(model_name="D2-law Bayesian parameter estimation",
            model_version="1.0", parameter="K", unit="m^2/s",
            posterior_mean=mean, credible_interval_95=(low, high),
            residual_sigma=noise_std_m2, sample_count=len(t),
            prior_description=f"Truncated Gaussian K>=0, base mean={prior_mean:g}, std={prior_std:g}; known D0 and noise",
            applicability=ApplicabilityResult(applicable=False, reasons=[
                "Applicability to this combustion regime must be independently verified before scientific interpretation"]),
            source_dataset_id=source_dataset_id,
            diagnostics={"rmse_diameter_squared_m2": float(np.sqrt(np.mean(residuals**2))),
                         "distinct_time_points": float(len(np.unique(t)))})
