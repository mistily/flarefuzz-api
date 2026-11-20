from fastapi.testclient import TestClient

from flarefuzz_api.main import app

client = TestClient(app)


def _provenanced(value: float, unit: str, source: str = "test fixture") -> dict[str, object]:
    return {"value": value, "unit": unit, "provenance": {"source": source}}


def test_first_order_kinetics_supports_bdf_and_radau():
    body = {
        "pre_exponential_factor_s_1": _provenanced(1e6, "s^-1"),
        "activation_energy_j_mol": _provenanced(8.0e4, "J/mol"),
        "temperature_k": _provenanced(1000.0, "K"),
        "initial_concentration_mol_m3": _provenanced(2.0, "mol/m^3"),
        "duration_s": 0.1,
        "sample_count": 11,
    }
    for solver in ("BDF", "Radau"):
        response = client.post("/api/analysis/kinetics/first-order", json={**body, "solver": solver})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["solver"] == solver
        assert result["rate_constant_s_1"] > 0
        assert result["points"][0]["concentration_mol_m3"] == 2.0
        assert result["points"][-1]["concentration_mol_m3"] < 2.0
        assert result["status"] == "illustrative_single_reaction_not_validated_mechanism"


def test_kinetics_rejects_wrong_units():
    body = {
        "pre_exponential_factor_s_1": _provenanced(1e6, "m/s"),
        "activation_energy_j_mol": _provenanced(8e4, "J/mol"),
        "temperature_k": _provenanced(1000.0, "K"),
        "initial_concentration_mol_m3": _provenanced(2.0, "mol/m^3"),
        "duration_s": 0.1,
    }
    response = client.post("/api/analysis/kinetics/first-order", json=body)
    assert response.status_code == 422


def test_extinction_strain_screen_abstains_without_configuration_limit():
    response = client.post("/api/analysis/extinction/strain-screen", json={
        "velocity_m_s": _provenanced(2.0, "m/s"),
        "characteristic_length_m": _provenanced(0.01, "m"),
    })
    assert response.status_code == 200
    result = response.json()
    assert result["strain_rate_s_1"] == 200.0
    assert result["status"] == "insufficient_evidence_no_extinction_limit"


def test_extinction_strain_screen_compares_against_supplied_tested_limit():
    response = client.post("/api/analysis/extinction/strain-screen", json={
        "velocity_m_s": _provenanced(2.0, "m/s"),
        "characteristic_length_m": _provenanced(0.01, "m"),
        "extinction_limit_s_1": _provenanced(150.0, "1/s", "bench test"),
    })
    assert response.status_code == 200
    assert response.json()["screening_comparison"] == "at_or_above_supplied_limit"
    assert response.json()["limit_provenance"]["source"] == "bench test"


def test_linear_gaussian_posterior_updates_prior_toward_observations():
    response = client.post("/api/analysis/inference/linear-gaussian", json={
        "x": [0.0, 1.0, 2.0, 3.0],
        "y": [1.0, 3.0, 5.0, 7.0],
        "prior_mean": [0.0, 0.0],
        "prior_covariance": [[100.0, 0.0], [0.0, 100.0]],
        "noise_std": 0.1,
        "data_provenance": {"source": "synthetic test"},
        "prior_provenance": {"source": "test prior"},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert abs(result["posterior_mean"][0] - 1.0) < 0.1
    assert abs(result["posterior_mean"][1] - 2.0) < 0.1
    assert result["posterior_family"] == "multivariate_normal"


def test_methods_catalog_marks_neural_operators_research_only():
    response = client.get("/api/analysis/methods")
    assert response.status_code == 200
    methods = {item["name"]: item for item in response.json()["methods"]}
    assert methods["Thermal transport"]["status"] == "available"
    assert methods["Neural operators"]["status"] == "research_not_implemented"
