"""Fit and retrieve trajectory simulations for one experimental run."""
from __future__ import annotations

from uuid import uuid4

import numpy as np
from fastapi import APIRouter, HTTPException

from flarefuzz_api.routes.datasets import DIAMETER_DATASETS
from flarefuzz_api.simulation.fitting import fit_quadratic_diameter, predict_quadratic
from flarefuzz_api.simulation.schemas import DiameterFitRequest

router = APIRouter(prefix="/api/simulations", tags=["simulations"])
DIAMETER_RUNS: dict[str, dict[str, object]] = {}


@router.post("/diameter/fit", status_code=201)
def fit_diameter_trajectory(request: DiameterFitRequest) -> dict[str, object]:
    dataset = DIAMETER_DATASETS.get(request.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Diameter dataset not found.")
    runs = dataset["parsed"]["runs"]
    run = runs.get(request.experiment_id)
    if run is None:
        raise HTTPException(status_code=422, detail="experiment_id must select one parsed run; runs are never combined.")
    t = run["time_s"]
    d = run["flame_diameter_m"]
    try:
        fitted = fit_quadratic_diameter(t, d, run["measurement_uncertainty_m"])
        prediction_end = max(t) + request.extrapolation_s
        prediction_times = np.linspace(min(t), prediction_end, request.prediction_points).tolist()
        predictions = predict_quadratic(fitted, prediction_times)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = str(uuid4())
    result: dict[str, object] = {
        "id": run_id,
        "model": "robust quadratic flame-diameter trajectory",
        "model_version": "quadratic-diameter-v1",
        "status": "prediction_within_observed_interval" if request.extrapolation_s == 0 else "prediction_includes_unvalidated_extrapolation",
        "dataset": {"dataset_id": request.dataset_id, "experiment_id": request.experiment_id,
                    "filename": dataset["filename"], "source_url": dataset["source_url"],
                    "nasa_accession": dataset["nasa_accession"]},
        "source_row_numbers": run["source_row_numbers"],
        "fit": {"origin_time_s": fitted.origin_time_s,
                "coefficients": {"D0_m": fitted.coefficients[0], "a_m_s": fitted.coefficients[1], "b_m_s2": fitted.coefficients[2]},
                "residual_scale_m": fitted.residual_scale_m, "weighted_by_measurement_uncertainty": fitted.weighted,
                "sample_count": fitted.sample_count, "rmse_m": fitted.rmse_m,
                "covariance": fitted.covariance},
        "observations": [{"row_number": row, "time_s": time, "diameter_m": diameter}
                         for row, time, diameter in zip(run["source_row_numbers"], t, d)],
        "predictions": predictions,
        "uncertainty": {"kind": "approximate pointwise 95% prediction interval",
                        "method": "delta-method parameter covariance plus residual scale; normal approximation",
                        "measurement_uncertainty_used": fitted.weighted},
        "units": {"time": "s", "diameter": "m", "a": "m/s", "b": "m/s^2"},
        "provenance": {"data_source_url": dataset["source_url"], "nasa_accession": dataset["nasa_accession"],
                       "source_row_numbers": run["source_row_numbers"], "model": "quadratic-diameter-v1"},
        "assumptions": [
            "D(t)=D0+a*(t-t_origin)+b*(t-t_origin)^2 is an empirical baseline, not a universal combustion law.",
            "The fit applies to one experiment only and is not extrapolated unless explicitly requested.",
            "When measurement uncertainties are absent, residual scale is estimated from fit residuals.",
            "Intervals are approximate and do not account for model misspecification or systematic measurement error.",
        ],
    }
    DIAMETER_RUNS[run_id] = result
    return result


@router.get("/diameter/runs/{run_id}")
def get_diameter_run(run_id: str) -> dict[str, object]:
    result = DIAMETER_RUNS.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Diameter simulation run not found.")
    return result
