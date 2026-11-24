from flarefuzz_api.simulation.cantera_properties import CanteraUnavailableError, gas_properties


def test_cantera_adapter_rejects_unallowlisted_mechanism_before_import():
    try:
        gas_properties(mechanism="/tmp/custom.yaml", temperature_k=300, pressure_pa=101325,
                       composition={"CH4": 0.5, "O2": 0.5})
    except ValueError as exc:
        assert "Unsupported mechanism" in str(exc)
