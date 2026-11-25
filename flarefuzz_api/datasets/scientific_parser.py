"""Structure-first CSV/TSV ingestion, preserving groups, roles and source positions.

Unknown units remain unknown; adjacent blocks are never paired implicitly.
"""
from __future__ import annotations

import csv
import io
import math
import re
from typing import Any


def _numeric(value: str) -> float | None:
    try:
        number = float(value.strip())
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _read(content: bytes, resource_format: str) -> list[list[str]]:
    text = content.decode("utf-8-sig")
    delimiter = "\t" if resource_format == "tsv" else ","
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def parse_scientific_table(content: bytes, *, resource_format: str = "csv") -> dict[str, Any]:
    """Normalize ordinary and repeated-block numeric tables without guessing units."""
    if resource_format not in {"csv", "tsv"}:
        raise ValueError("Only CSV/TSV tables are supported")
    rows = _read(content, resource_format)
    if len(rows) < 2 or not any(cell.strip() for row in rows[1:] for cell in row):
        raise ValueError("No observations")
    headers = rows[0]
    width = max(map(len, rows))
    headers = headers + [""] * (width - len(headers))
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for i in range(width + 1):
        occupied = i < width and bool(headers[i].strip())
        if occupied and start is None:
            start = i
        elif not occupied and start is not None:
            blocks.append((start, i))
            start = None
    if not blocks:
        raise ValueError("No column headers")
    observations: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for block_index, (first, last) in enumerate(blocks):
        group_label = headers[first].strip()
        # A repeated header (e.g. C7) is a group label only when
        # the first data cell describes a role (Expt/Sim), not a measurement.
        first_value = rows[1][first].strip() if first < len(rows[1]) else ""
        role = {"expt": "experimental", "experiment": "experimental",
                "sim": "simulated", "simulation": "simulated"}.get(first_value.casefold())
        grouped = role is not None and last - first > 1
        group = {"block_index": block_index, "label": group_label if grouped else None,
                 "role": role, "start_column": first + 1, "end_column": last}
        groups.append(group)
        for row_number, row in enumerate(rows[1:], start=2):
            variables: dict[str, float] = {}
            columns: dict[str, int] = {}
            for column in range(first + (1 if grouped else 0), last):
                raw = row[column] if column < len(row) else ""
                number = _numeric(raw)
                if number is None:
                    continue
                name = headers[column].strip()
                if not name:
                    continue
                # Preserve unknown names; duplicate names remain distinguishable by block.
                variables[name] = number
                columns[name] = column + 1
            if variables:
                observations.append({"variables": variables,
                                     "units": {key: None for key in variables},
                                     "dimensions": {"group": group["label"], "role": role,
                                                    "block_index": block_index},
                                     "source_row": row_number, "source_columns": columns})
    if not observations:
        raise ValueError("No finite numeric observations")
    return {"dataset_category": "structured_measurements",
            "observations": observations, "groups": groups,
            "issues": [], "fitting_compatible": False,
            "source_row_number_convention": "1-based physical resource rows including header"}
