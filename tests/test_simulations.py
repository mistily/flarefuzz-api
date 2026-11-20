from fastapi.testclient import TestClient

from flarefuzz_api.main import app

client = TestClient(app)


def _provenanced(value: float, source: str = "controlled experiment") -> dict[str, object]:
    return {"value": value, "provenance": {"source": source, "reference": "test fixture"}}


def _request() -> dict[str, object]:
    return {
        "detection_time": "2026-11-20T12:00:00Z",
        "initial_droplet_diameter_m": _provenanced(1e-3),
        "burning_constant_m2_s": _provenanced(1e-7, "independent fit"),
        "flame_to_droplet_diameter_ratio": _provenanced(3.0, "image measurement"),
        "ambient_temperature_k": _provenanced(298.15, "thermocouple"),
        "ambient_pressure_pa": _provenanced(101325.0, "pressure sensor"),
        "oxygen_mole_fraction": _provenanced(0.21, "gas analyzer"),
        "ambient_velocity_m_s": _provenanced(0.0, "anemometer"),
        "sample_count": 3,
    }


def test_spherical_droplet_flame_returns_envelope_over_remaining_lifetime():
    response = client.post("/api/simulations/spherical-droplet-flame", json=_request())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "illustrative_prediction_not_validated"
    assert result["predicted_remaining_burnout_duration_s"] == 10.0
    assert len(result["points"]) == 3
    assert result["points"][0]["droplet_diameter_m"] == 1e-3
    assert result["points"][0]["flame_envelope"]["diameter_m"] == 3e-3
    assert result["points"][-1]["droplet_diameter_m"] == 0
    assert result["points"][-1]["flame_envelope"]["volume_m3"] == 0
    assert result["environment"]["use"].startswith("fixed environmental context")
    assert result["provenance"]["input_sources"]["oxygen_mole_fraction"]["source"] == "gas analyzer"
    assert "not a validated prediction of flame extinction" in result["assumptions"][-1]


def test_spherical_droplet_flame_requires_timezone_and_ratio_above_one():
    body = _request()
    body["detection_time"] = "2026-11-20T12:00:00"
    assert client.post("/api/simulations/spherical-droplet-flame", json=body).status_code == 422

    body = _request()
    body["flame_to_droplet_diameter_ratio"] = _provenanced(1.0)
    assert client.post("/api/simulations/spherical-droplet-flame", json=body).status_code == 422


def test_spherical_droplet_flame_rejects_nonpositive_burning_constant():
    body = _request()
    body["burning_constant_m2_s"] = _provenanced(0.0)
    assert client.post("/api/simulations/spherical-droplet-flame", json=body).status_code == 422
