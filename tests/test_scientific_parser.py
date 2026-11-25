"""Regression coverage for general scientific table layouts."""
from flarefuzz_api.datasets.diameter_parser import parse_diameter_resource
from flarefuzz_api.datasets.quality import classify_diameter_dataset


def test_repeated_grouped_experimental_and_simulated_blocks():
    csv_data = (
        "C7,Do,Dext_HF,Dext_CF,,C7,Do,Dext_HF,Dext_CF\n"
        "Expt,3.91,3.28,1.3,,Sim,2,0,0\n"
        ",3.557,2.794,1.214,,,3,0,0\n"
    ).encode()
    result = parse_diameter_resource(csv_data)
    assert result["dataset_category"] == "structured_measurements"
    assert len(result["observations"]) == 4
    assert result["groups"][0]["role"] == "experimental"
    assert result["groups"][1]["role"] == "simulated"
    assert result["observations"][0]["source_columns"]["Do"] == 2
    assert result["observations"][0]["units"]["Do"] is None
    assert result["fitting_compatible"] is False
    assert classify_diameter_dataset(result)["playback_compatible"] is False


def test_simple_numeric_table_retains_unknown_variables():
    result = parse_diameter_resource(b"Pressure,Signal\\n1,2\\n3,4\\n".replace(b"\\n", b"\n"))
    assert result["dataset_category"] == "structured_measurements"
    assert len(result["observations"]) == 2
