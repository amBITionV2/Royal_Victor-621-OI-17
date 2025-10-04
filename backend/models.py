"""Pydantic data models for incidents, metrics, and action responses."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Metric(BaseModel):
    """Represents a single telemetry datapoint associated with an incident."""

    name: str
    value: float
    unit: Optional[str] = None
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class Incident(BaseModel):
    """Incoming incident payload for root-cause analysis."""

    id: str
    service: str
    severity: Literal["low", "medium", "high", "critical"]
    description: str
    metrics: list[Metric] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    reported_at: datetime = Field(default_factory=datetime.utcnow)


class RCAResult(BaseModel):
    """Structured output from root-cause analysis."""

    incident_id: str
    summary: str
    probable_causes: list[str]
    remediation_steps: list[str]
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    """Desired workflow action request from the frontend or automation."""

    incident_id: str
    workflow: Literal["restart", "rollback", "scale_up"]
    parameters: dict[str, Any] = Field(default_factory=dict)


class ActionResponse(BaseModel):
    """Response object describing the outcome of a workflow trigger."""

    workflow: str
    status: Literal["queued", "triggered", "error"]
    message: str
    run_url: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
