"""Parse diameter time-series and experimental/simulated comparison CSVs."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any


class DiameterCSVError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_TIME_FACTORS = {"s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
                 "ms": 1e-3, "millisecond": 1e-3, "milliseconds": 1e-3,
                 "min": 60.0, "minute": 60.0, "minutes": 60.0}
_DIAMETER_FACTORS = {"m": 1.0, "meter": 1.0, "meters": 1.0,
                     "mm": 1e-3, "millimeter": 1e-3, "millimeters": 1e-3,
                     "cm": 1e-2, "centimeter": 1e-2, "centimeters": 1e-2,
                     "um": 1e-6, "µm": 1e-6, "micrometer": 1e-6}


def _header_parts(header: str) -> tuple[str, str | None]:
    match = re.search(r"[\[(]\s*([^\])]+?)\s*[\])]\s*$", header)
    if not match:
        return header.strip().casefold(), None
    return header[:match.start()].strip().casefold(), match.group(1).strip().casefold().replace("μ", "µ")


def _key_and_unit(header: str) -> tuple[str, str | None]:
    base, unit = _header_parts(header)
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_"), unit


def _canonical_column(headers: list[str], names: set[str]) -> tuple[int, str | None] | None:
    for index, header in enumerate(headers):
        key, unit = _key_and_unit(header)
        if key in names:
            return index, unit
    return None


def _read_csv(content: bytes, delimiter: str | None = None) -> tuple[list[str], list[list[str]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DiameterCSVError("invalid_encoding", "CSV/TSV must be UTF-8 encoded.") from exc
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise DiameterCSVError("not_time_series", "Resource is empty.") from exc
    return headers, list(reader)


@dataclass(frozen=True)
class DiameterRun:
    experiment_id: str
    time_s: list[float]
    flame_diameter_m: list[float]
    source_row_numbers: list[int]
    measurement_uncertainty_m: list[float] | None


def _parse_time_series(headers: list[str], rows: list[list[str]], experiment_id: str | None) -> dict[str, Any]:
    time_column = _canonical_column(headers, {"time", "time_s", "elapsed_time", "elapsed_time_s", "elapsed_time_seconds"})
    diameter_column = _canonical_column(headers, {"flame_diameter", "flame_diameter_m", "diameter", "diameter_m"})
    if time_column is None or diameter_column is None:
        raise DiameterCSVError("not_time_series", "CSV must contain recognizable time and flame-diameter columns.")
    ti, t_unit = time_column
    di, d_unit = diameter_column
    if t_unit is None or t_unit not in _TIME_FACTORS:
        raise DiameterCSVError("unknown_time_unit", "Time column must declare a supported unit (s, ms, or min).")
    if d_unit is None or d_unit not in _DIAMETER_FACTORS:
        raise DiameterCSVError("unknown_diameter_unit", "Flame-diameter column must declare a supported unit (m, cm, mm, or µm).")
    run_column = _canonical_column(headers, {"experiment_id", "experiment", "run_id", "run", "test_id"})
    uncertainty_column = _canonical_column(headers, {"measurement_uncertainty", "measurement_uncertainty_m", "diameter_uncertainty", "diameter_uncertainty_m", "sigma_diameter"})
    groups: dict[str, dict[str, list[Any]]] = {}
    issues: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        run_id = experiment_id or (row[run_column[0]].strip() if run_column and len(row) > run_column[0] else "run-1")
        if not run_id:
            issues.append({"row": row_number, "issue": "missing_experiment_id"})
            continue
        try:
            t = float(row[ti].strip()) * _TIME_FACTORS[t_unit]
            diameter = float(row[di].strip()) * _DIAMETER_FACTORS[d_unit]
            sigma = None
            if uncertainty_column and len(row) > uncertainty_column[0] and row[uncertainty_column[0]].strip():
                uncertainty_unit = uncertainty_column[1] or d_unit
                if uncertainty_unit not in _DIAMETER_FACTORS:
                    raise ValueError("unsupported uncertainty unit")
                sigma = float(row[uncertainty_column[0]].strip()) * _DIAMETER_FACTORS[uncertainty_unit]
            if not (isfinite(t) and t >= 0 and isfinite(diameter) and diameter > 0):
                raise ValueError("time must be finite/nonnegative and diameter finite/positive")
            if sigma is not None and (not isfinite(sigma) or sigma <= 0):
                raise ValueError("measurement uncertainty must be finite and positive")
        except (ValueError, IndexError) as exc:
            issues.append({"row": row_number, "issue": "invalid_measurement", "detail": str(exc)})
            continue
        group = groups.setdefault(run_id, {"time": [], "diameter": [], "rows": [], "sigma": []})
        group["time"].append(t); group["diameter"].append(diameter); group["rows"].append(row_number); group["sigma"].append(sigma)
    runs: dict[str, dict[str, Any]] = {}
    for run_id, group in groups.items():
        if len(group["time"]) < 3 or len(set(group["time"])) < 3:
            issues.append({"experiment_id": run_id, "issue": "insufficient_distinct_time_points"})
            continue
        if all(value is None for value in group["sigma"]):
            uncertainty = None
        elif any(value is None for value in group["sigma"]):
            issues.append({"experiment_id": run_id, "issue": "incomplete_uncertainty_column"})
            uncertainty = None
        else:
            uncertainty = [float(value) for value in group["sigma"]]
        runs[run_id] = {"experiment_id": run_id, "time_s": group["time"],
                        "flame_diameter_m": group["diameter"], "source_row_numbers": group["rows"],
                        "measurement_uncertainty_m": uncertainty}
    if not runs:
        raise DiameterCSVError("not_time_series", "No individual experiment contains at least three valid, distinct time points.")
    return {"dataset_category": "diameter_time_series", "runs": runs, "issues": issues,
            "source_row_number_convention": "1-based physical resource rows, including the header row",
            "time_unit": "s", "diameter_unit": "m"}


def _column_by_pattern(headers: list[str], patterns: tuple[str, ...]) -> tuple[int, str | None] | None:
    for index, header in enumerate(headers):
        key, unit = _key_and_unit(header)
        if any(re.search(pattern, key) for pattern in patterns):
            return index, unit
    return None


def _parse_comparison(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    experimental = _column_by_pattern(headers, (r"^(?:experimental|experiment|measured|observed)_?(?:flame_)?diameter$", r"^diameter_?(?:experimental|measured|observed)$"))
    simulated = _column_by_pattern(headers, (r"^(?:simulated|simulation|predicted|model)_?(?:flame_)?diameter$", r"^diameter_?(?:simulated|predicted|model)$"))
    if experimental is None or simulated is None:
        raise DiameterCSVError("unsupported_dataset_category", "CSV is neither a supported diameter time series nor an experimental/simulated diameter comparison.")
    ei, eu = experimental; si, su = simulated
    if eu is None or eu not in _DIAMETER_FACTORS or su is None or su not in _DIAMETER_FACTORS:
        raise DiameterCSVError("unknown_diameter_unit", "Experimental and simulated diameter columns must declare supported units.")
    if abs(_DIAMETER_FACTORS[eu] - _DIAMETER_FACTORS[su]) > 1e-15:
        # Distinct units are accepted and converted, preserving each source unit.
        pass
    x_column = _column_by_pattern(headers, (r"^(?:time|time_s|elapsed_time)(?:_.*)?$", r"^(?:pressure|pressure_pa|temperature|temperature_k|equivalence_ratio|phi|strain_rate)(?:_.*)?$", r"^(?:condition|case|sample|point)_?(?:id)?$"))
    run_column = _canonical_column(headers, {"experiment_id", "experiment", "run_id", "run", "test_id"})
    comparisons: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        try:
            experimental_m = float(row[ei].strip()) * _DIAMETER_FACTORS[eu]
            simulated_m = float(row[si].strip()) * _DIAMETER_FACTORS[su]
            if not (isfinite(experimental_m) and experimental_m > 0 and isfinite(simulated_m) and simulated_m > 0):
                raise ValueError("diameters must be finite and positive")
            x_value: object = None
            x_name: str | None = None
            x_unit: str | None = None
            if x_column:
                xi, xu = x_column
                raw = row[xi].strip()
                if raw:
                    try:
                        x_value = float(raw)
                        if not isfinite(x_value):
                            raise ValueError("comparison coordinate is non-finite")
                    except ValueError:
                        x_value = raw
                    x_name, x_unit = _key_and_unit(headers[xi])
        except (ValueError, IndexError) as exc:
            issues.append({"row": row_number, "issue": "invalid_comparison", "detail": str(exc)})
            continue
        comparisons.append({"source_row_number": row_number, "experiment_id": row[run_column[0]].strip() if run_column and len(row) > run_column[0] else None,
                            "coordinate_name": x_name, "coordinate_unit": x_unit, "coordinate": x_value,
                            "experimental_diameter_m": experimental_m, "simulated_diameter_m": simulated_m,
                            "error_m": simulated_m - experimental_m, "absolute_error_m": abs(simulated_m - experimental_m)})
    if not comparisons:
        raise DiameterCSVError("empty_comparison", "No valid experimental/simulated diameter pairs found.")
    errors = [item["error_m"] for item in comparisons]
    return {"dataset_category": "extinction_diameter_comparison", "comparisons": comparisons,
            "metrics": {"sample_count": len(comparisons), "bias_m": sum(errors) / len(errors),
                        "mae_m": sum(abs(value) for value in errors) / len(errors),
                        "rmse_m": (sum(value * value for value in errors) / len(errors)) ** 0.5},
            "issues": issues, "diameter_unit": "m",
            "source_row_number_convention": "1-based physical resource rows, including the header row"}


def parse_diameter_resource(content: bytes, *, resource_format: str = "csv", experiment_id: str | None = None) -> dict[str, Any]:
    if resource_format not in {"csv", "tsv"}:
        raise DiameterCSVError("unsupported_resource_format", "Diameter parser supports CSV and TSV resources.")
    headers, rows = _read_csv(content, "\t" if resource_format == "tsv" else None)
    has_time = _canonical_column(headers, {"time", "time_s", "elapsed_time", "elapsed_time_s", "elapsed_time_seconds"}) is not None
    has_diameter = _canonical_column(headers, {"flame_diameter", "flame_diameter_m", "diameter", "diameter_m"}) is not None
    if has_time and has_diameter:
        return _parse_time_series(headers, rows, experiment_id)
    return _parse_comparison(headers, rows)


def parse_diameter_csv(content: bytes, *, experiment_id: str | None = None) -> dict[str, Any]:
    """Backward-compatible CSV parser; returns either supported diameter category."""
    return parse_diameter_resource(content, resource_format="csv", experiment_id=experiment_id)
