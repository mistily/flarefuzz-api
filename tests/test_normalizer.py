import pytest

from flarefuzz_api.models import DatasetMetadata
from flarefuzz_api.normalizer import inventory_csv, normalized_csv_response


@pytest.mark.parametrize(
    ("header", "si_unit", "factor"),
    [
        ("Flame diameter (mm)", "m", 0.001),
        ("K (mm²/sec)", "m^2/s", 1e-6),
        ("Lag (s/mm²)", "s/m^2", 1e6),
        ("Pressure (atm)", "Pa", 101325.0),
        ("(D/D0)^2", "dimensionless", 1.0),
        ("Dflame/Ddrop", "dimensionless", 1.0),
    ],
)
def test_known_unit_conversion_factor(header, si_unit, factor):
    csv = f"{header}\n2\n".encode()
    inventory, data = inventory_csv(csv)
    parameter = inventory.parameters[0]
    assert parameter.si_unit == si_unit
    assert parameter.conversion_factor == pytest.approx(factor)
    assert float(data.attrs["si_values"].iloc[0, 0]) == pytest.approx(2 * factor)


def test_mm_conversion_value():
    inventory, data = inventory_csv(b"Flame diameter (mm)\n12\n")
    assert inventory.parameters[0].si_unit == "m"
    assert data.attrs["si_values"].iloc[0, 0] == pytest.approx(0.012)


def test_unknown_unit_is_not_converted():
    inventory, _ = inventory_csv(b"Flame diameter (furlong)\n2\n")
    parameter = inventory.parameters[0]
    assert parameter.si_unit is None
    assert parameter.conversion_factor is None
    assert parameter.validation_status == "manual_review"


def test_ambiguous_k_and_watts_are_flagged():
    inventory, _ = inventory_csv(b"K (W)\n3\n")
    assert inventory.parameters[0].validation_status == "manual_review"
    inventory, _ = inventory_csv(b"Power (W)\n3\n")
    assert inventory.parameters[0].validation_status == "manual_review"


def test_kinematic_viscosity_is_not_guessed_to_be_burning_constant():
    inventory, _ = inventory_csv(b"Kinematic viscosity (mm^2/sec)\n3\n")
    assert inventory.parameters[0].canonical_name is None
    assert inventory.parameters[0].validation_status == "manual_review"


def test_multilevel_headers_preserve_repeated_series_and_cool_flame():
    csv = (
        "Experiment,,\n"
        "Flame diameter (mm),Flame diameter (mm),Cool flame diameter (mm)\n"
        "1,2,3\n"
    ).encode()
    inventory, data = inventory_csv(csv)
    assert [item.source_column_index for item in inventory.parameters] == [0, 1, 2]
    assert inventory.parameters[0].canonical_name == "flame_diameter"
    assert inventory.parameters[0].series_identifier == "flame_diameter#1"
    assert inventory.parameters[1].series_identifier == "flame_diameter#2"
    assert inventory.parameters[1].notes[-1] == "Repeated measurement retained as a separate series."
    assert inventory.parameters[2].canonical_name == "cool_flame_diameter"
    assert data.columns.tolist() == ["flame_diameter#1", "flame_diameter#2", "cool_flame_diameter#1"]


def test_ratio_types_remain_distinct_and_experimental_metadata_is_preserved():
    metadata = DatasetMetadata(provenance="experimental", experiment="drop-tower-A")
    inventory, _ = inventory_csv(b"(D/D0)^2,Dflame/Ddrop\n0.7,1.2\n", metadata=metadata)
    assert inventory.parameters[0].canonical_name == "diameter_ratio_squared"
    assert inventory.parameters[1].canonical_name == "flame_to_droplet_diameter_ratio"
    assert inventory.metadata.experiment == "drop-tower-A"
    assert inventory.metadata.provenance == "experimental"


def test_burning_constant_is_provisional_not_thermal_diffusivity():
    inventory, _ = inventory_csv(b"K (mm2/sec)\n1\n")
    parameter = inventory.parameters[0]
    assert parameter.canonical_name == "burning_or_evaporation_constant"
    assert parameter.validation_status == "provisional"
    assert "not thermal diffusivity" in parameter.notes[0]


def test_source_and_si_data_are_both_available():
    inventory, data = inventory_csv(b"Flame diameter (mm)\n12\n")
    assert data.iloc[0, 0] == "12"
    assert data.attrs["si_values"].iloc[0, 0] == pytest.approx(0.012)
    response = normalized_csv_response(inventory, data)
    assert response.source_values == [{"flame_diameter#1": "12"}]
    assert response.si_values == [{"flame_diameter#1": pytest.approx(0.012)}]
def test_missing_measurement_is_reported():
    inventory, data = inventory_csv(
        b"Flame diameter (mm)\n\n12\n"
    )
    response = normalized_csv_response(inventory, data)

    print("Validation issues:", response.validation_issues)

    assert any(
        issue.issue_type == "missing_value"
        for issue in response.validation_issues
    )


def test_invalid_numeric_measurement_is_reported():
    inventory, data = inventory_csv(
        b"Flame diameter (mm)\nabc\n"
    )

    response = normalized_csv_response(inventory, data)

    assert response.validation_issues[0].issue_type == "invalid_numeric"


def test_infinite_measurement_is_reported():
    inventory, data = inventory_csv(
        b"Flame diameter (mm)\ninf\n"
    )

    response = normalized_csv_response(inventory, data)

    assert response.validation_issues[0].issue_type == "non_finite"


def test_valid_measurement_has_no_validation_issues():
    inventory, data = inventory_csv(
        b"Flame diameter (mm)\n12\n"
    )

    response = normalized_csv_response(inventory, data)

    assert response.validation_issues == []
def test_multilevel_header_when_first_row_has_recognized_parameter():
    csv = (
        "Flame diameter (mm),Cool flame diameter (mm)\n"
        "(mm),(mm)\n"
        "12,3\n"
    ).encode()

    inventory, data = inventory_csv(csv)

    assert len(inventory.parameters) == 2
    assert inventory.parameters[0].canonical_name == "flame_diameter"
    assert inventory.parameters[1].canonical_name == "cool_flame_diameter"
    assert len(data) == 1
    assert data.iloc[0, 0] == "12"


def test_explicit_header_rows_override_is_preserved():
    csv = (
        "Flame diameter (mm)\n"
        "(mm)\n"
        "12\n"
    ).encode()

    inventory, data = inventory_csv(csv, header_rows=1)

    assert len(data) == 2
    assert data.iloc[0, 0] == "(mm)"


def test_categorical_metadata_is_not_numeric_validated():
    csv = (
        "Fuel,Flame diameter (mm)\n"
        "Methanol,12\n"
    ).encode()

    inventory, data = inventory_csv(csv)
    response = normalized_csv_response(inventory, data)

    assert not any(
        issue.series_identifier.startswith("fuel")
        for issue in response.validation_issues
    )
    assert response.validation_issues == []


def test_empty_separator_column_is_not_reported_as_missing():
    csv = (
        "Fuel,,Flame diameter (mm)\n"
        "Methanol,,12\n"
    ).encode()

    inventory, data = inventory_csv(csv)
    response = normalized_csv_response(inventory, data)

    assert response.validation_issues == []