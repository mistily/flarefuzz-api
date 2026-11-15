from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Provenance(str, Enum):
    EXPERIMENTAL = "experimental"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


class DatasetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: Provenance = Provenance.UNKNOWN
    experiment: str | None = None
    simulation: str | None = None
    source_url: str | None = None


class ParameterInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_column_index: int = Field(ge=0)
    source_header: str
    canonical_name: str | None
    source_unit: str | None
    si_unit: str | None
    conversion_factor: float | None
    series_identifier: str
    validation_status: Literal["validated", "provisional", "manual_review"]
    notes: list[str] = Field(default_factory=list)


class InventoryResponse(BaseModel):
    metadata: DatasetMetadata
    parameters: list[ParameterInventoryItem]

class DataQualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_row_index: int = Field(ge=0)
    series_identifier: str
    issue_type: Literal[
        "missing_value",
        "invalid_numeric",
        "non_finite"
    ]
    raw_value: str
    message: str
class NormalizedCSVResponse(InventoryResponse):
    source_values: list[dict[str, str]]
    si_values: list[dict[str, float | None]]
    validation_issues: list[DataQualityIssue] = Field(default_factory=list)


class GatheredInventory(BaseModel):
    project_title: str | None = None
    project_id: str | None = None
    resource_url: str
    inventory: InventoryResponse
