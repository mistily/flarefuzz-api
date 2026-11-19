from fastapi.testclient import TestClient

from flarefuzz_api.main import app

client = TestClient(app)


def _input(value: float, source: str = "test fixture") -> dict[str, object]:
    return {"value": value, "provenance": {"source": source, "reference": "tests/test_calculations.py"}}


def test_dimensionless_calculation_returns_groups_units_assumptions_and_lineage():
    response = client.post("/api/calculations/dimensionless", json={
        "density_kg_m3": _input(1.2),
        "velocity_m_s": _input(2.0),
        "characteristic_length_m": _input(0.1),
        "dynamic_viscosity_pa_s": _input(1.81e-5),
        "specific_heat_capacity_j_kg_k": _input(1005.0),
        "thermal_conductivity_w_m_k": _input(0.026),
        "mass_diffusivity_m2_s": _input(2e-5),
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stage"] == "dimensionless_analysis"
    assert result["results"]["Re"]["value"] == 1.2 * 2.0 * 0.1 / 1.81e-5
    assert round(result["results"]["Pr"]["value"], 3) == 0.700
    assert result["results"]["Sc"]["value"] == 1.81e-5 / (1.2 * 2e-5)
    assert result["results"]["Le"]["value"] == 0.026 / (1.2 * 1005.0 * 2e-5)
    assert all(item["unit"] == "1" for item in result["results"].values())
    assert result["results"]["Pr"]["input_provenance"]["dynamic_viscosity_pa_s"]["source"] == "test fixture"
    assert result["provenance"]["formula_set"] == "dimensionless-transport-v1"
    assert result["assumptions"]


def test_dimensionless_calculation_requires_positive_transport_properties():
    body = {
        "density_kg_m3": _input(1.2),
        "velocity_m_s": _input(2.0),
        "characteristic_length_m": _input(0.1),
        "dynamic_viscosity_pa_s": _input(0.0),
        "specific_heat_capacity_j_kg_k": _input(1005.0),
        "thermal_conductivity_w_m_k": _input(0.026),
        "mass_diffusivity_m2_s": _input(2e-5),
    }
    assert client.post("/api/calculations/dimensionless", json=body).status_code == 422


def test_formula_catalog_describes_intermediate_stage_and_equations():
    response = client.get("/api/calculations/dimensionless/formulas")
    assert response.status_code == 200
    result = response.json()
    assert result["position"] == "after normalization and before simulation"
    assert result["formulas"]["Pr"]["equation"] == "mu * cp / k"
    assert result["required_units"]["dynamic_viscosity_pa_s"] == "Pa*s"
