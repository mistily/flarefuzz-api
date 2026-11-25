"""Quality classification for diameter CSV ingestion."""
from __future__ import annotations

from typing import Any


def classify_diameter_dataset(parsed: dict[str, Any] | None = None, *, error_code: str | None = None) -> dict[str, Any]:
    if error_code == "not_time_series":
        return {"status": "not_time_series", "playback_compatible": False,
                "message": "This dataset does not contain a verified diameter-versus-time trajectory."}
    if parsed and parsed.get("dataset_category") == "structured_measurements":
        return {"status": "structured_measurements", "playback_compatible": False,
                "message": "Measurements preserved; no verified time trajectory or automatic fitting.",
                "issues": parsed.get("issues", [])}
    if not parsed or not parsed.get("runs"):
        return {"status": "invalid", "playback_compatible": False, "issues": [] if not parsed else parsed.get("issues", [])}
    return {"status": "time_series", "playback_compatible": True,
            "experiment_ids": sorted(parsed["runs"]), "issues": parsed.get("issues", [])}
