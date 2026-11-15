
from __future__ import annotations

import re
from collections import Counter
from io import BytesIO
from math import isfinite

import pandas as pd
import pint

from flarefuzz_api.models import (
    DatasetMetadata,
    DataQualityIssue,
    InventoryResponse,
    NormalizedCSVResponse,
    ParameterInventoryItem,
)

ureg = pint.UnitRegistry()

_UNIT_ALIASES = {
    "mm": "millimeter",
    "mm2/s": "millimeter**2 / second",
    "mm^2/s": "millimeter**2 / second",
    "mm2/sec": "millimeter**2 / second",
    "mm^2/sec": "millimeter**2 / second",
    "s/mm2": "second / millimeter**2",
    "s/mm^2": "second / millimeter**2",
    "sec/mm2": "second / millimeter**2",
    "sec/mm^2": "second / millimeter**2",
    "atm": "standard_atmosphere",
    "": "dimensionless",
    "1": "dimensionless",
    "dimensionless": "dimensionless",
}

_SI_UNITS = {
    "mm": "m",
    "mm2/s": "m^2/s",
    "mm^2/s": "m^2/s",
    "mm2/sec": "m^2/s",
    "mm^2/sec": "m^2/s",
    "s/mm2": "s/m^2",
    "s/mm^2": "s/m^2",
    "sec/mm2": "s/m^2",
    "sec/mm^2": "s/m^2",
    "atm": "Pa",
    "": "dimensionless",
    "1": "dimensionless",
    "dimensionless": "dimensionless",
}

METADATA = re.compile(r"^(?:fuel|reference|data\s*file|experiment|simulation|run|sample|oxidizer|notes?)$", re.I)

_ALIASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cool_flame_diameter",
        re.compile(
            r"\b(?:cool\s*flame|coolflame)\s*(?:diameter|d)\b|\bd(?:cool|cf)\b",
            re.I,
        ),
    ),
    (
        "flame_to_droplet_diameter_ratio",
        re.compile(
            r"\bdflame\s*/\s*ddrop\b|"
            r"\bflame\s*diameter\s*/\s*(?:droplet|drop)\s*diameter\b",
            re.I,
        ),
    ),
    (
        "diameter_ratio_squared",
        re.compile(
            r"\(?\s*d\s*/\s*d0\s*\)?\s*\^?\s*2|"
            r"\(?\s*d\s*/\s*d₀\s*\)?\s*²",
            re.I,
        ),
    ),
    (
        "flame_diameter",
        re.compile(r"\b(?:flame\s*diameter|dflame|flame\s*d)\b", re.I),
    ),
    (
        "droplet_diameter",
        re.compile(r"\b(?:droplet|drop)\s*diameter\b|\bddrop\b", re.I),
    ),
    (
        "burning_or_evaporation_constant",
        re.compile(r"^k$", re.I),
    ),
    (
        "power",
        re.compile(r"\b(?:radiant\s*power|radiation\s*power|power)\b", re.I),
    ),
    (
        "gas_composition",
        re.compile(
            r"\b(?:gas\s*)?composition\b|\b(?:mole|mass)\s*fraction\b",
            re.I,
        ),
    ),
)


def _header_text(parts: list[str]) -> str:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return " | ".join(dict.fromkeys(cleaned))


def _split_unit(header: str) -> tuple[str, str | None]:
    match = re.search(r"[\[(]\s*([^\]\)]+?)\s*[\])]\s*$", header)

    if not match:
        return header.strip(), None

    return header[:match.start()].strip(), match.group(1).strip()


def _normalized_unit(unit: str | None) -> str | None:
    if unit is None:
        return None

    return re.sub(r"\s+", "", unit.lower()).replace("²", "2")


def _parameter_identity(
    header: str,
) -> tuple[str | None, str | None, list[str], str]:

    base, source_unit = _split_unit(header)

    notes: list[str] = []
    status = "validated"
    canonical: str | None = None

    for name, pattern in _ALIASES:
        label = base.split("|")[-1].strip()

        matches = (
            pattern.fullmatch(label)
            if name == "burning_or_evaporation_constant"
            else pattern.search(base)
        )

        if matches:
            canonical = name
            break

    normalized_unit = _normalized_unit(source_unit)

    if canonical == "burning_or_evaporation_constant":
        if normalized_unit in {
            "mm2/sec",
            "mm^2/sec",
            "mm2/s",
            "mm^2/s",
        }:
            status = "provisional"
            notes.append(
                "K is treated as a provisional D-squared-law "
                "burning/evaporation constant, not thermal diffusivity."
            )
        else:
            status = "manual_review"
            notes.append(
                "K is ambiguous without a verified parameter definition and unit."
            )

    if canonical == "power":
        status = "manual_review"
        notes.append(
            "Power in watts is not identified as physical radiance without verification."
        )

    if canonical == "gas_composition":
        status = "manual_review"
        notes.append(
            "Composition basis is unverified; do not assume mole fraction."
        )

    if canonical is None:
        status = "manual_review"
        notes.append("Header does not match a verified parameter alias.")

    if source_unit is not None and normalized_unit not in _UNIT_ALIASES:
        status = "manual_review"
        notes.append("Unknown unit; no automatic conversion was applied.")

    elif source_unit is None and canonical not in {
        "diameter_ratio_squared",
        "flame_to_droplet_diameter_ratio",
    }:
        status = "manual_review"
        notes.append("No source unit was present in the header.")

    return canonical, source_unit or None, notes, status


def _conversion(
    source_unit: str | None,
    canonical_name: str | None,
) -> tuple[str | None, float | None]:

    if source_unit is None and canonical_name in {
        "diameter_ratio_squared",
        "flame_to_droplet_diameter_ratio",
    }:
        return "dimensionless", 1.0

    normalized = _normalized_unit(source_unit)

    if normalized not in _UNIT_ALIASES:
        return None, None

    source = _UNIT_ALIASES[normalized]
    target = _SI_UNITS[normalized]

    factor = (1 * ureg(source)).to(target).magnitude

    return target, float(factor)


def _make_headers(rows: list[list[str]]) -> list[str]:
    width = max((len(row) for row in rows), default=0)

    padded = [
        row + [""] * (width - len(row))
        for row in rows
    ]

    for row_index in range(max(0, len(padded) - 1)):
        previous = ""

        for column_index, value in enumerate(padded[row_index]):
            if value.strip():
                previous = value.strip()
            elif previous:
                padded[row_index][column_index] = previous

    headers: list[str] = []

    for index in range(width):
        headers.append(
            _header_text([row[index] for row in padded])
        )

    return headers


def _guess_header_row_count(
    rows: list[list[str]],
    max_header_rows: int = 5,
) -> int:
    """Detect grouped headers, including headers with recognized first-row labels."""

    if not rows:
        return 1

    if len(rows) < 2 or max_header_rows < 2:
        return 1

    second_row = rows[1]

    if not any(cell.strip() for cell in second_row):
        return 1

    # A second row containing recognized parameter labels is header-like.
    for cell in second_row:
        canonical, _, _, _ = _parameter_identity(cell.strip())
        if canonical is not None:
            return 2

        # Also recognize unit-only subheaders such as (mm) or (atm).
        _, unit = _split_unit(cell.strip())
        if (
            unit is not None
            and _normalized_unit(unit) in _UNIT_ALIASES
        ):
            return 2

    return 1

def _is_number(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except ValueError:
        return False


def inventory_csv(
    content: bytes,
    metadata: DatasetMetadata | None = None,
    header_rows: int | None = None,
) -> tuple[InventoryResponse, pd.DataFrame]:
    """Interpret CSV headers and return an inventory plus source data."""

    raw = pd.read_csv(
        BytesIO(content),
        header=None,
        dtype=str,
        keep_default_na=False,
        skip_blank_lines=False,
    )

    raw = raw.fillna("")
    rows = raw.values.tolist()

    if not rows:
        return (
            InventoryResponse(
                metadata=metadata or DatasetMetadata(),
                parameters=[],
            ),
            pd.DataFrame(),
        )

    count = (
        header_rows
        if header_rows is not None
        else _guess_header_row_count(rows)
    )

    if count < 1 or count > len(rows):
        raise ValueError(
            "header_rows must be between 1 and the number of CSV rows"
        )

    headers = _make_headers(rows[:count])

    data = raw.iloc[count:].reset_index(drop=True)

    occurrences: Counter[str] = Counter()
    interpreted_parameters: list[ParameterInventoryItem] = []

    for index, header in enumerate(headers):
        canonical, source_unit, notes, status = _parameter_identity(header)

        key = (
            canonical
            or re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")
            or f"column_{index}"
        )

        occurrences[key] += 1
        series_id = f"{key}#{occurrences[key]}"

        if occurrences[key] > 1:
            notes.append(
                "Repeated measurement retained as a separate series."
            )

        interpreted_parameters.append(
            ParameterInventoryItem(
                source_column_index=index,
                source_header=header,
                canonical_name=canonical,
                source_unit=source_unit,
                si_unit=None,
                conversion_factor=None,
                series_identifier=series_id,
                validation_status=status,
                notes=notes,
            )
        )

    parameters: list[ParameterInventoryItem] = []

    for parameter in interpreted_parameters:
        si_unit, conversion_factor = _conversion(
            parameter.source_unit,
            parameter.canonical_name,
        )

        parameters.append(
            parameter.model_copy(
                update={
                    "si_unit": si_unit,
                    "conversion_factor": conversion_factor,
                }
            )
        )

    data.columns = [
        parameter.series_identifier
        for parameter in parameters
    ]

    si_data = data.copy()

    for parameter in parameters:
        if parameter.conversion_factor is None:
            continue

        values = pd.to_numeric(
            data[parameter.series_identifier],
            errors="coerce",
        )

        si_data[parameter.series_identifier] = (
            values * parameter.conversion_factor
        )

    data.attrs["si_values"] = si_data

    data.attrs["parameter_inventory"] = [
        parameter.model_dump()
        for parameter in parameters
    ]

    return (
        InventoryResponse(
            metadata=metadata or DatasetMetadata(),
            parameters=parameters,
        ),
        data,
    )


def normalized_csv_response(
    inventory: InventoryResponse,
    data: pd.DataFrame,
) -> NormalizedCSVResponse:

    source_values = [
        {
            str(key): str(value)
            for key, value in row.items()
        }
        for row in data.to_dict(orient="records")
    ]

    si_values: list[dict[str, float | None]] = []

    for _, row in data.iterrows():
        converted: dict[str, float | None] = {}

        for parameter in inventory.parameters:
            value = row[parameter.series_identifier]

            try:
                if (
                    parameter.conversion_factor is None
                    or not str(value).strip()
                ):
                    converted[parameter.series_identifier] = None
                else:
                    result = (
                        float(value) * parameter.conversion_factor
                    )

                    converted[parameter.series_identifier] = (
                        result if isfinite(result) else None
                    )

            except (TypeError, ValueError):
                converted[parameter.series_identifier] = None

        si_values.append(converted)

    validation_issues: list[DataQualityIssue] = []

    for row_index, row in data.iterrows():
        for parameter in inventory.parameters:

        # Skip categorical metadata and intentionally empty separator columns.
            is_measurement = (
                parameter.canonical_name is not None
                or parameter.source_unit is not None
            )

            if not is_measurement:
                continue

            raw_value = str(
                row[parameter.series_identifier]
            ).strip()

            if not raw_value:
                issue_type = "missing_value"
                message = "Measurement value is missing."
            else:
                try:
                    numeric_value = float(raw_value)
                except ValueError:
                    issue_type = "invalid_numeric"
                    message = "Measurement is not a valid number."
                else:
                    if not isfinite(numeric_value):
                        issue_type = "non_finite"
                        message = "Measurement must be finite."
                    else:
                        continue

            validation_issues.append(
                DataQualityIssue(
                    data_row_index=int(row_index),
                    series_identifier=parameter.series_identifier,
                    issue_type=issue_type,
                    raw_value=raw_value,
                    message=message,
                )
            )
        

    return NormalizedCSVResponse(
        metadata=inventory.metadata,
        parameters=inventory.parameters,
        source_values=source_values,
        si_values=si_values,
        validation_issues=validation_issues,
    )
