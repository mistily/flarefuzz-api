# API contract reconciliation: diameter fit

The existing React-facing ingestion, quality, series, model-fit, and model-run
paths remain in place. `POST /api/models/fit` now supports both the legacy
`linear` request and an opt-in `quadratic` flame-diameter request. The quadratic
path uses the normalized dataset stored by `POST /api/datasets/ingest`; it
requires exactly one validated time series in seconds and one validated flame
diameter series in meters. Upload one experiment per dataset and identify it
with `experiment_id`; the service never pools runs.

```json
{
  "dataset_id": "<id returned by ingest>",
  "experiment_id": "run-001",
  "investigation_id": "PSI-117",
  "model": "quadratic",
  "target": "flame_diameter_m",
  "uncertainty": {"method": "bootstrap", "confidence_level": 0.95, "iterations": 500},
  "prediction_points": 120
}
```

The returned model run has `run_id`, `status`, `model`, `data_kind`, source
identifiers, fitted parameters, RMSE, prediction arrays in meters/seconds, and
limitations. Fetch the same result with `GET /api/models/runs/{run_id}`. With no
extrapolation requested, prediction samples stay within the measured time span.
Bootstrap bands are pointwise residual-bootstrap intervals; they do not capture
model misspecification or systematic uncertainty. The numbers in this document
are contract examples, not NASA measurements.

Optional `POST /api/simulations/{run_id}/transport` accepts a curated Cantera
mechanism, temperature, pressure, composition, and composition provenance. It
requires an existing model run and returns thermophysical properties only; it
does not change/refit the diameter trajectory or claim CFD coupling. Cantera
must be installed separately, and arbitrary mechanism paths are rejected.
