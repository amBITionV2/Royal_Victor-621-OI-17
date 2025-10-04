"""Gemini-powered root-cause analysis logic for HealOps incidents."""

from __future__ import annotations

import logging
from typing import Any

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
        print("Response: ", response)
        parsed = _parse_response(response)
        return models.RCAResult(**parsed, incident_id=incident.id)
            
    except Exception as e:
        logger.error(f"Gemini API error: {e}, falling back to heuristic RCA")
        return _heuristic_rca(incident)


def _parse_response(response: Any) -> dict[str, Any]:
    """Transform the Gemini response into the RCAResult schema."""
    # Extract text content from the Gemini response
    text_content = ""
    if hasattr(response, 'text'):
        text_content = response.text
    elif hasattr(response, 'candidates') and response.candidates:
        candidate = response.candidates[0]
        if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
            text_content = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
    
    return {
        "summary": text_content or "No summary available.",
        "probable_causes": [],
        "remediation_steps": [],
        "confidence": 0.5,
        "metadata": {
            "raw_response": str(response),  # Convert to string instead of trying to serialize the object
        },
    }


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
