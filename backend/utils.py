"""Utility helpers for logging, environment access, and prompt generation."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

logger = logging.getLogger("healops")
logging.basicConfig(level=os.getenv("HEALOPS_LOG_LEVEL", "INFO"))


def get_env(key: str, default: str | None = None) -> str | None:
    """Return an environment variable value with an optional default."""
    value = os.getenv(key, default)
    if value is None:
        logger.debug("Environment variable %s is not set", key)
    return value


def build_rca_prompt(incident: Any) -> str:
    """Create a prompt for Gemini summarizing the incident context."""
    parts: Iterable[str] = [
        "You are AutoMedic, an SRE assistant performing root-cause analysis.",
        f"Incident ID: {incident.id}",
        f"Service: {incident.service}",
        f"Severity: {incident.severity}",
        "Description:",
        incident.description,
        "Metrics:",
        "\n".join(f"- {metric.name}: {metric.value}{metric.unit or ''}" for metric in incident.metrics) or "(none)",
        "Logs:",
        "\n".join(incident.logs) or "(none)",
        "Provide probable causes, remediation steps, and a confidence score between 0 and 1.",
    ]
    prompt = "\n".join(parts)
    logger.debug("Constructed RCA prompt: %s", prompt)
    return prompt


def safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely extract an attribute or dictionary key from a response object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def to_serializable(obj: Any) -> Any:
    """Attempt to convert arbitrary objects into JSON-serializable structures."""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if hasattr(obj, "to_dict"):
            return obj.to_dict()  # type: ignore[no-any-return]
        if hasattr(obj, "__dict__"):
            return obj.__dict__
    return str(obj)
