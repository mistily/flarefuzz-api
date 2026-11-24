from fastapi.testclient import TestClient

from flarefuzz_api.main import app
from flarefuzz_api.routes.datasets import DIAMETER_DATASETS
from flarefuzz_api.routes.simulations import DIAMETER_RUNS

client = TestClient(app)


def setup_function():
    DIAMETER_DATASETS.clear()
    DIAMETER_RUNS.clear()


def test_ingest_and_fit_one_run_then_retrieve_run():
    csv_bytes = (b"time_s,flame_diameter_mm,experiment_id\n0,10,A\n1,11.1,A\n2,12.8,A\n3,15.2,A\n"
                 b"0,100,B\n1,101,B\n2,102,B\n")
    ingest = client.post("/api/datasets/diameter/ingest", files={"file": ("diameters.csv", csv_bytes, "text/csv")})
    assert ingest.status_code == 201, ingest.text
    dataset = ingest.json()
    assert dataset["quality"]["status"] == "time_series"
    fit = client.post("/api/simulations/diameter/fit", json={
        "dataset_id": dataset["id"], "experiment_id": "A", "prediction_points": 7,
    })
    assert fit.status_code == 201, fit.text
    result = fit.json()
    assert len(result["observations"]) == 4
    assert len(result["predictions"]) == 7
    assert result["dataset"]["experiment_id"] == "A"
    assert result["predictions"][0]["lower_95_m"] <= result["predictions"][0]["diameter_m"]
    retrieved = client.get(f"/api/simulations/diameter/runs/{result['id']}")
    assert retrieved.json() == result


def test_fit_rejects_unknown_run_instead_of_combining_experiments():
    ingest = client.post("/api/datasets/diameter/ingest", files={
        "file": ("diameters.csv", b"time_s,flame_diameter_mm,run_id\n0,10,A\n1,11,A\n2,13,A\n", "text/csv")})
    dataset_id = ingest.json()["id"]
    response = client.post("/api/simulations/diameter/fit", json={"dataset_id": dataset_id, "experiment_id": "B"})
    assert response.status_code == 422


def test_non_timeseries_ingest_reports_structured_status():
    response = client.post("/api/datasets/diameter/ingest", files={
        "file": ("summary.csv", b"pressure_Pa,flame_diameter_mm\n100000,12\n", "text/csv")})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_time_series"
