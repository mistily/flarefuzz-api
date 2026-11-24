"""Reusable reduced-order flame-diameter trajectory models."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import numpy as np
from scipy.optimize import least_squares


@dataclass(frozen=True)
class QuadraticFit:
    origin_time_s: float
    coefficients: tuple[float, float, float]
    covariance: list[list[float]]
    residual_scale_m: float
    weighted: bool
    sample_count: int
    rmse_m: float


def fit_quadratic_diameter(time_s: list[float], diameter_m: list[float],
                           uncertainty_m: list[float] | None = None) -> QuadraticFit:
    t = np.asarray(time_s, dtype=float)
    d = np.asarray(diameter_m, dtype=float)
    if t.ndim != 1 or d.ndim != 1 or len(t) != len(d) or len(t) < 3:
        raise ValueError("At least three paired time/diameter values are required.")
    if not (np.isfinite(t).all() and np.isfinite(d).all()) or (t < 0).any() or (d <= 0).any():
        raise ValueError("Times and diameters must be finite; times nonnegative and diameters positive.")
    if len(np.unique(t)) < 3:
        raise ValueError("Quadratic fitting requires at least three distinct timestamps.")
    origin = float(t.min())
    x = t - origin
    design = np.column_stack((np.ones_like(x), x, x*x))
    weighted = uncertainty_m is not None
    if weighted:
        sigma = np.asarray(uncertainty_m, dtype=float)
        if sigma.shape != d.shape or not np.isfinite(sigma).all() or (sigma <= 0).any():
            raise ValueError("Measurement uncertainties must be positive finite values paired with observations.")
        scale = sigma
    else:
        scale = np.ones_like(d)
    initial = np.linalg.lstsq(design / scale[:, None], d / scale, rcond=None)[0]
    fit = least_squares(lambda theta: (design @ theta - d) / scale, initial,
                        loss="soft_l1", f_scale=1.0, max_nfev=5000)
    if not fit.success or not np.isfinite(fit.x).all() or not np.isfinite(fit.jac).all():
        raise ValueError("Robust quadratic fit did not converge.")
    raw_residuals = d - design @ fit.x
    dof = max(1, len(d) - 3)
    residual_scale = float(np.sqrt(np.sum(raw_residuals**2) / dof))
    try:
        covariance_base = np.linalg.pinv(fit.jac.T @ fit.jac)
    except np.linalg.LinAlgError as exc:
        raise ValueError("Could not estimate fit covariance.") from exc
    covariance = covariance_base if weighted else covariance_base * residual_scale**2
    if not (isfinite(residual_scale) and np.isfinite(covariance).all()):
        raise ValueError("Fit uncertainty was numerically non-finite.")
    return QuadraticFit(origin, tuple(float(v) for v in fit.x), covariance.tolist(),
                        residual_scale, weighted, len(t), float(np.sqrt(np.mean(raw_residuals**2))))


def predict_quadratic(fit: QuadraticFit, time_s: list[float]) -> list[dict[str, float]]:
    t = np.asarray(time_s, dtype=float)
    if t.ndim != 1 or not np.isfinite(t).all() or (t < fit.origin_time_s).any():
        raise ValueError("Prediction times must be finite and at or after the fit origin.")
    x = t - fit.origin_time_s
    design = np.column_stack((np.ones_like(x), x, x*x))
    theta = np.asarray(fit.coefficients)
    covariance = np.asarray(fit.covariance)
    predicted = design @ theta
    result = []
    for time, row, value in zip(t, design, predicted):
        prediction_variance = max(0.0, float(row @ covariance @ row))
        half_width = 1.96 * (prediction_variance + fit.residual_scale_m**2) ** 0.5
        result.append({"time_s": float(time), "diameter_m": float(value),
                       "lower_95_m": max(0.0, float(value) - half_width),
                       "upper_95_m": float(value) + half_width})
    return result
