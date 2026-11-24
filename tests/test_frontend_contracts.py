from fastapi.testclient import TestClient

from flarefuzz_api.frontend import _DATASETS, _RUNS
from flarefuzz_api.main import app
from flarefuzz_api.models import NormalizedCSVResponse, ParameterInventoryItem, DatasetMetadata
from flarefuzz_api.routes.transport import router as transport_router

client = TestClient(app)


def _headers() -> dict[str, object]:
    return {"dataset_id": "demo", "experiment_id": "run-001", "model": "quadratic", "target": "flame_diameter_m",
            "uncertainty": {"method": "bootstrap", "confidence_level": 0.95, "iterations": 50}, "prediction_points": 3}


def setup_function():
    _DATASETS.clear()
    _RUNS.clear()


def test_contract_paths_fit_and_retrieve_quadratic_prediction():
    parameters = [
        ParameterInventoryItem(source_column_index=0, source_header="Time (s)", canonical_name="time", source_unit="s", si_unit="s", conversion_factor=1, series_identifier="time#1", validation_status="validated"),
        ParameterInventoryItem(source_column_index=1, source_header="Flame diameter (m)", canonical_name="flame_diameter", source_unit="m", si_unit="m", conversion_factor=1, series_identifier="flame_diameter#1", validation_status="validated"),
    ]
    _DATASETS["demo"] = NormalizedCSVResponse(metadata=DatasetMetadata(), parameters=parameters,
        source_values=[], si_values=[{"time#1": 0.0, "flame_diameter#1": .004},
                                     {"time#1": 1.0, "flame_diameter#1": .00419},
                                     {"time#1": 2.0, "flame_diameter#1": .00436},
                                     {"time#1": 3.0, "flame_diameter#1": .00451}])
    quality = client.get("/api/datasets/demo/quality")
    series = client.get("/api/datasets/demo/series")
    assert quality.status_code == series.status_code == 200
    fit = client.post("/api/models/fit", json=_headers())
    assert fit.status_code == 201, fit.text
    result = fit.json()
    assert result["run_id"] and result["status"] == "completed"
    assert result["data_kind"] == "model_prediction"
    assert result["source"]["experiment_id"] == "run-001"
    assert len(result["predictions"]["timeS"]) == 3
    assert len(result["predictions"]["lowerFlameDiameterM"]) == 3
    assert result["limitations"]
    assert client.get(f"/api/models/runs/{result['run_id']}").json() == result


def test_quadratic_contract_rejects_missing_or_ambiguous_measurements():
    assert client.post("/api/models/fit", json=_headers()).status_code == 404


def test_transport_contract_requires_an_existing_run(monkeypatch):
    response = client.post("/api/simulations/missing/transport", json={
        "mechanism": "gri30.yaml", "temperature_k": 1000, "pressure_pa": 101325,
        "composition": {"CH4": 0.1, "O2": 0.2, "N2": 0.7}, "composition_provenance": "test gas analysis"})
    assert response.status_code == 404
