"""Gemini-powered root-cause analysis logic for HealOps incidents."""

from __future__ import annotations

import logging
from typing import Any

import json
import re

from google import generativeai as genai

from . import models, utils

logger = logging.getLogger("healops.rca")


def configure_client() -> bool:
    """Configure the Gemini SDK with the provided API key.

    Returns False when the key is missing so callers can fallback gracefully.
    """

    key = utils.get_env("GEMINI_API_KEY")

    if not key:
        logger.warning("GEMINI_API_KEY missing; falling back to heuristic RCA")
        return False

    genai.configure(api_key=key)
    return True


def run_root_cause_analysis(incident: models.Incident) -> models.RCAResult:
    """Generate RCA insights for an incident using Gemini when available."""
    if not configure_client():
        logger.info("Using heuristic RCA (no Gemini API key)")
        return _heuristic_rca(incident)

    try:
        prompt = utils.build_rca_prompt(incident)
        model = genai.GenerativeModel(utils.get_env("GEMINI_MODEL", "gemini-2.0-flash"))

        response = model.generate_content(prompt)
        parsed = _parse_response(response)
        return models.RCAResult(**parsed, incident_id=incident.id)

    except Exception as exc:  # noqa: BLE001
        logger.error("Gemini API error: %s, falling back to heuristic RCA", exc)
        return _heuristic_rca(incident)


def _parse_response(response: Any) -> dict[str, Any]:
    """Transform the Gemini response into the RCAResult schema."""

    text_content = _extract_text(response)
    structured = _maybe_parse_structured(text_content)

    summary = text_content or "No summary available."
    probable_causes: list[str] = []
    remediation_steps: list[str] = []
    recommended_action: str | None = None
    confidence = None

    if isinstance(structured, dict):
        probable_causes = _as_list(structured.get("probable_causes"))
        remediation_steps = _as_list(structured.get("remediation_steps"))
        recommended_action = _coerce_str(structured.get("recommended_action"))
        confidence = _coerce_confidence(structured.get("confidence"))
        # allow summary override from structured data if provided
        summary = _coerce_str(structured.get("summary")) or summary
    else:
        probable_causes = _extract_list_from_text(text_content, "probable cause")
        remediation_steps = _extract_list_from_text(text_content, "remediation")

    if confidence is None:
        confidence = _extract_confidence_from_text(text_content) or 0.5

    metadata: dict[str, Any] = {
        "raw_response": str(response),
        "confidence_source": "gemini" if confidence != 0.5 else "default",
    }
    if structured and isinstance(structured, dict):
        metadata["structured"] = structured

    return {
        "summary": summary,
        "probable_causes": probable_causes,
        "remediation_steps": remediation_steps,
        "confidence": confidence,
        "metadata": metadata,
        "recommended_action": recommended_action,
    }


def _extract_text(response: Any) -> str:
    if hasattr(response, "text") and isinstance(response.text, str):
        return response.text

    if hasattr(response, "candidates") and response.candidates:
        candidate = response.candidates[0]
        parts = getattr(candidate, "content", None)
        if parts and hasattr(parts, "parts"):
            return "".join(
                part.text for part in parts.parts if hasattr(part, "text") and isinstance(part.text, str)
            )

    return ""


def _maybe_parse_structured(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(code_blocks)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return [str(value)]


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _coerce_confidence(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(raw))
        if not match:
            return None
        value = float(match.group(1))
        if "%" in str(raw):
            value /= 100.0
    if value > 1:
        value /= 100.0
    return max(0.0, min(value, 1.0))


def _extract_confidence_from_text(text: str) -> float | None:
    if not text:
        return None

    pattern = re.compile(
        r"confidence(?:\s*(?:score|level|estimate|rating))?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*(%?)",
        flags=re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        value = float(match.group(1))
        if match.group(2) == "%" or value > 1:
            value /= 100.0
        return max(0.0, min(value, 1.0))

    # fallback: look for phrases like "80% confidence" or "confidence of 0.8"
    pattern_alt = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(%?)\s*confidence", flags=re.IGNORECASE)
    match_alt = pattern_alt.search(text)
    if match_alt:
        value = float(match_alt.group(1))
        if match_alt.group(2) == "%" or value > 1:
            value /= 100.0
        return max(0.0, min(value, 1.0))

    return None


def _extract_list_from_text(text: str, heading_keyword: str) -> list[str]:
    if not text:
        return []

    pattern = re.compile(
        rf"{heading_keyword}s?\s*[:\-]\s*(.*)",
        flags=re.IGNORECASE,
    )
    matches = pattern.findall(text)
    if not matches:
        return []

    items: list[str] = []
    for match in matches:
        parts = re.split(r"\n|;|,", match)
        items.extend(part.strip(" -*•") for part in parts if part.strip())
    return items


def _heuristic_rca(incident: models.Incident) -> models.RCAResult:
    """Provide a simple RCA when LLM access is unavailable."""
    metrics = {metric.name: metric.value for metric in incident.metrics}
    error_rate = float(metrics.get("error_rate", 0.0))
    p95_ms = float(metrics.get("p95_ms", 0.0))

    probable_causes: list[str]
    remediation_steps: list[str]
    recommended_action = "restart"
    confidence = 0.4

    if error_rate >= 0.2:
        probable_causes = ["Spike in application errors, likely bad release"]
        remediation_steps = ["rollback", "increase logging"]
        recommended_action = "rollback"
        confidence = 0.6
    elif p95_ms >= 1500:
        probable_causes = ["High latency suggests resource saturation"]
        remediation_steps = ["scale_up", "restart pod"]
        recommended_action = "scale_up"
        confidence = 0.5
    else:
        probable_causes = ["Minor service instability detected"]
        remediation_steps = ["restart", "monitor metrics"]

    summary = (
        "Heuristic RCA based on current metrics. Provide GEMINI_API_KEY for upgraded insights."
    )

    return models.RCAResult(
        incident_id=incident.id,
        summary=summary,
        probable_causes=probable_causes,
        remediation_steps=remediation_steps,
        confidence=confidence,
        metadata={
            "recommended_action": recommended_action,
            "source": "heuristic",
        },
    )
