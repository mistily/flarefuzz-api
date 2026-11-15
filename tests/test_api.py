from fastapi.testclient import TestClient

from flarefuzz_api.frontend import _DATASETS, _RUNS
from flarefuzz_api.main import app

client = TestClient(app)


def setup_function():
    _DATASETS.clear()
    _RUNS.clear()


def test_normalize_endpoint_returns_inventory_and_separate_si_values():
    response = client.post(
        "/api/v1/normalize",
        files={"file": ("diameters.csv", b"Flame diameter (mm)\n12\n", "text/csv")},
        data={"provenance": "experimental", "experiment": "drop-tower-A"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["parameters"][0]["canonical_name"] == "flame_diameter"
    assert result["source_values"] == [{"flame_diameter#1": "12"}]
    assert result["si_values"] == [{"flame_diameter#1": 0.012}]
    assert result["metadata"]["experiment"] == "drop-tower-A"


def test_normalize_endpoint_rejects_unregistered_formats():
    response = client.post(
        "/api/v1/normalize",
        files={"file": ("dataset.xlsx", b"not csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 415


def _ingest(content: bytes = b"Time (s),Flame diameter (mm)\n0,10\n1,12\n2,14\n3,16\n") -> str:
    response = client.post(
        "/api/datasets/ingest",
        files={"file": ("data.csv", content, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_ingest_quality_and_series_endpoints_share_dataset():
    dataset_id = _ingest()
    quality = client.get(f"/api/datasets/{dataset_id}/quality")
    series = client.get(f"/api/datasets/{dataset_id}/series")
    assert quality.status_code == series.status_code == 200
    assert quality.json()["dataset_id"] == dataset_id
    assert series.json()["row_count"] == 4
    flame = next(item for item in series.json()["series"] if item["series_identifier"] == "flame_diameter#1")
    assert flame["values"] == [0.01, 0.012, 0.014, 0.016]


def test_model_fit_persists_run_and_reports_residual_uncertainty():
    dataset_id = _ingest()
    response = client.post("/api/models/fit", json={
        "dataset_id": dataset_id,
        "model": "linear",
        "x_series": "time#1",
        "y_series": "flame_diameter#1",
    })
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["sample_count"] == 4
    assert run["slope"] > 0
    assert run["uncertainty_95_approx"] >= 0
    assert client.get(f"/api/models/runs/{run['id']}").json() == run


def test_questions_only_answer_with_matching_series_evidence():
    dataset_id = _ingest()
    response = client.post("/api/questions", json={
        "dataset_id": dataset_id,
        "question": "What is the range of flame diameter?",
    })
    assert response.status_code == 200
    answer = response.json()
    assert answer["grounded"] is True
    assert answer["evidence"][0]["minimum"] == 0.01
    assert answer["evidence"][0]["maximum"] == 0.016

    abstention = client.post("/api/questions", json={"dataset_id": dataset_id, "question": "Why did it happen?"})
    assert abstention.json()["grounded"] is False


def test_unknown_dataset_returns_404():
    assert client.get("/api/datasets/missing/series").status_code == 404
    assert client.get("/api/models/runs/missing").status_code == 404
