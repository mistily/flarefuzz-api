"""Parse diameter-vs-time CSVs without mixing experimental runs."""
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


def _canonical_column(headers: list[str], names: set[str]) -> tuple[int, str | None] | None:
    for index, header in enumerate(headers):
        base, unit = _header_parts(header)
        key = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
        if key in names:
            return index, unit
    return None


@dataclass(frozen=True)
class DiameterRun:
    experiment_id: str
    time_s: list[float]
    flame_diameter_m: list[float]
    source_row_numbers: list[int]
    measurement_uncertainty_m: list[float] | None


def parse_diameter_csv(content: bytes, *, experiment_id: str | None = None) -> dict[str, Any]:
    """Return validated runs; CSV headers must state supported units explicitly."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DiameterCSVError("invalid_encoding", "CSV must be UTF-8 encoded.") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise DiameterCSVError("not_time_series", "CSV is empty.") from exc
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
    for row_number, row in enumerate(reader, start=2):
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
        group["time"].append(t)
        group["diameter"].append(diameter)
        group["rows"].append(row_number)
        group["sigma"].append(sigma)

    runs: dict[str, DiameterRun] = {}
    for run_id, group in groups.items():
        if len(group["time"]) < 3 or len(set(group["time"])) < 3:
            issues.append({"experiment_id": run_id, "issue": "insufficient_distinct_time_points"})
            continue
        if len(set(group["sigma"])) == 1 and group["sigma"][0] is None:
            uncertainty = None
        elif any(value is None for value in group["sigma"]):
            issues.append({"experiment_id": run_id, "issue": "incomplete_uncertainty_column"})
            uncertainty = None
        else:
            uncertainty = [float(value) for value in group["sigma"]]
        runs[run_id] = DiameterRun(run_id, group["time"], group["diameter"], group["rows"], uncertainty)
    if not runs:
        raise DiameterCSVError("not_time_series", "No individual experiment contains at least three valid, distinct time points.")
    return {
        "runs": {key: {
            "experiment_id": run.experiment_id,
            "time_s": run.time_s,
            "flame_diameter_m": run.flame_diameter_m,
            "source_row_numbers": run.source_row_numbers,
            "measurement_uncertainty_m": run.measurement_uncertainty_m,
        } for key, run in runs.items()},
        "issues": issues,
        "source_row_number_convention": "1-based physical CSV rows, including the header row",
        "time_unit": "s",
        "diameter_unit": "m",
    }
