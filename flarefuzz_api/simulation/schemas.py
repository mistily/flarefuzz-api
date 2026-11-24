"""Request schemas for simulation endpoints."""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field


class DiameterFitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    prediction_points: int = Field(default=101, ge=3, le=1000)
    extrapolation_s: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    robust_loss: str = Field(default="soft_l1", pattern="^soft_l1$")
