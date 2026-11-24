from pathlib import Path

import pytest

from flarefuzz_api.datasets.diameter_parser import DiameterCSVError, parse_diameter_csv

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_diameter.csv"


def test_parse_diameter_csv_preserves_runs_rows_and_converts_si():
    parsed = parse_diameter_csv(FIXTURE.read_bytes())
    assert set(parsed["runs"]) == {"run-A", "run-B"}
    run = parsed["runs"]["run-A"]
    assert run["time_s"] == [0.0, 1.0, 2.0, 3.0]
    assert run["flame_diameter_m"] == [0.01, 0.011, 0.013, 0.016]
    assert run["measurement_uncertainty_m"] == [0.0002] * 4
    assert run["source_row_numbers"] == [2, 3, 4, 5]
    assert len(parsed["runs"]["run-B"]["time_s"]) == 3


def test_non_timeseries_is_classified_not_time_series():
    with pytest.raises(DiameterCSVError) as error:
        parse_diameter_csv(b"pressure_Pa,flame_diameter_mm\n100000,12\n")
    assert error.value.code == "not_time_series"


def test_missing_or_unknown_units_are_rejected():
    with pytest.raises(DiameterCSVError) as missing:
        parse_diameter_csv(b"time,flame diameter (mm)\n0,10\n1,11\n2,12\n")
    assert missing.value.code == "unknown_time_unit"


def test_runs_with_too_few_points_are_not_used():
    with pytest.raises(DiameterCSVError):
        parse_diameter_csv(b"time_s,flame_diameter_mm,run_id\n0,10,A\n1,11,A\n2,12,B\n")
