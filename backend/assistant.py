"""Conversational assistant that maps natural language into HealOps actions."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict

from google import generativeai as genai

from . import actions, models, rca, utils
from .health_probe import aggregate, apply_fault_overlay, sample_once

logger = logging.getLogger("healops.assistant")

ERROR_RATE_MAX = float(os.getenv("ERROR_RATE_MAX", "0.05"))
P95_MS_MAX = int(os.getenv("P95_MS_MAX", "1000"))

DEFAULT_SERVICE = os.getenv("ASSISTANT_SERVICE", "demo-api")
DEFAULT_NAMESPACE = os.getenv("ASSISTANT_NAMESPACE", "default")
DEFAULT_REGION = os.getenv("ASSISTANT_REGION", "ap-south-1")
DEFAULT_ENV = os.getenv("ASSISTANT_ENV", "prod")
DEFAULT_HOST_PORT = os.getenv("ASSISTANT_HOST_PORT", "8080")
DEFAULT_CONTAINER_PORT = os.getenv("ASSISTANT_CONTAINER_PORT", "80")

SUPPORTED_INTENTS = {"check_health", "explain_latency", "restart_service", "greeting", "fallback"}

MOCK_KNOWLEDGE_BASE = {
    "services": {
        "demo-api": {
            "owner": "Team Atlas",
            "description": "Customer order intake API running on EKS",
            "primary_region": "ap-south-1",
            "dependencies": ["payments-api", "catalog-cache"],
            "runbook_link": "https://wiki.internal/runbooks/demo-api",
            "sla": "99.9% availability, p95 latency < 800ms",
        }
    },
    "incidents": [
        {
            "id": "INC-217",
            "date": "2025-10-03",
            "summary": "Latency spike traced to noisy neighbour on shared node",
            "remediation": ["Cordon problematic node", "Scale out to 6 replicas"],
        },
        {
            "id": "INC-204",
            "date": "2025-09-28",
            "summary": "Error burst after bad config deploy",
            "remediation": ["Rollback config", "Purge Redis cache"],
        },
    ],
    "metrics": {
        "yesterday": {
            "p95_ms": 1450,
            "error_rate": 0.032,
            "notes": "Traffic surge from marketing campaign caused short-term saturation",
        },
        "last_deploy": {
            "version": "v2.14.3",
            "completed_at": "2025-10-04T06:30:00Z",
            "changes": ["Increased db pool", "Added circuit breaker to upstream"],
        },
    },
}

PARSER_SYSTEM_PROMPT_TEMPLATE = """
You are AutoMedic's command parser. Convert each user instruction into a JSON object
with the following schema:
{
  "intent": "check_health|explain_latency|restart_service|greeting|fallback",
  "confidence": <number between 0 and 1>,
  "parameters": {
      "service": optional string,
      "environment": optional string,
      "threshold": optional number,
      "reason": optional string
  },
  "reply": "Very short acknowledgement in natural language"
}
Pick the intent that best matches the instruction. Use "fallback" when unsure.
You have access to the following knowledge base:
```json
{knowledge_base}
```
Use it to ground parameters when possible. Respond with JSON only. Do not include markdown fences or commentary.
"""


def handle_message(message: str) -> models.AssistantResponse:
    """Interpret the user's message and execute the corresponding intent."""

    logger.info("Assistant received message: %s", message)
    plan = _interpret_message(message)
    intent = plan.get("intent", "fallback")
    confidence = float(plan.get("confidence") or 0.0)
    parameters: Dict[str, Any] = plan.get("parameters") or {}
    acknowledgement = plan.get("reply")
    logger.info("Assistant parsed intent=%s confidence=%.2f params=%s", intent, confidence, parameters)

    if intent not in SUPPORTED_INTENTS:
        intent = "fallback"

    context = _build_context(intent, parameters)
    context.setdefault("debug", {})
    context["debug"].update(
        {
            "plan": plan,
            "acknowledgement": acknowledgement,
        }
    )
    took_action = False

    if intent == "restart_service":
        result = _dispatch_restart(parameters)
        context["action_result"] = result
        took_action = result.get("ok", False)

    message_text = _generate_response_text(
        original_message=message,
        intent=intent,
        acknowledgement=acknowledgement,
        confidence=confidence,
        context=context,
    )

    return models.AssistantResponse(
        intent=intent,
        message=message_text,
        confidence=confidence,
        took_action=took_action,
        context=context,
    )


def _interpret_message(message: str) -> dict[str, Any]:
    if not message.strip():
        return {"intent": "fallback", "confidence": 0.0, "parameters": {}, "reply": ""}

    if not rca.configure_client():
        logger.warning("Gemini unavailable for assistant parsing; defaulting to fallback")
        return {"intent": "fallback", "confidence": 0.0, "parameters": {}, "reply": "Gemini is offline."}

    try:
        model_name = utils.get_env("GEMINI_MODEL", "gemini-2.0-flash")
        parser_prompt = PARSER_SYSTEM_PROMPT_TEMPLATE.format(knowledge_base=json.dumps(MOCK_KNOWLEDGE_BASE))
        model = genai.GenerativeModel(model_name, system_instruction=parser_prompt, generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(message)
        blob = _extract_first_json(response)
        if isinstance(blob, dict):
            return blob
    except Exception as exc:  # noqa: BLE001
        logger.error("Assistant parse failure: %s", exc)
        return {
            "intent": "fallback",
            "confidence": 0.0,
            "parameters": {},
            "reply": "I couldn't understand that.",
            "error": str(exc),
        }

    return {"intent": "fallback", "confidence": 0.0, "parameters": {}, "reply": "I couldn't understand that."}


def _extract_first_json(response: Any) -> dict[str, Any] | None:
    text = ""
    if hasattr(response, "text") and isinstance(response.text, str):
        text = response.text
    elif hasattr(response, "candidates") and response.candidates:
        candidate = response.candidates[0]
        parts = getattr(candidate, "content", None)
        if parts and hasattr(parts, "parts"):
            text = "".join(
                part.text for part in parts.parts if hasattr(part, "text") and isinstance(part.text, str)
            )

    if not text:
        return None

    # Attempt direct load
    stripped = text.strip()
    candidates = []
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(code_blocks)

    candidates.extend(_extract_balanced_json(text))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    logger.debug("Assistant parser could not decode JSON, raw text: %s", text)
    kv_guess = _parse_key_value_lines(text)
    if kv_guess:
        return kv_guess
    return None


def _extract_balanced_json(text: str) -> list[str]:
    results: list[str] = []
    stack = []
    start_idx = None
    for idx, char in enumerate(text):
        if char == "{":
            stack.append(char)
            if len(stack) == 1:
                start_idx = idx
        elif char == "}":
            if stack:
                stack.pop()
                if not stack and start_idx is not None:
                    candidate = text[start_idx : idx + 1]
                    results.append(candidate)
                    start_idx = None
    return results


def _parse_key_value_lines(text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines() if ":" in line]
    if not lines:
        return None
    result: dict[str, Any] = {}
    for line in lines:
        match = re.match(r'"?(?P<key>[A-Za-z0-9_]+)"?\s*[:=]\s*(?P<value>.+)', line)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value").strip().strip(",")
        value = value.strip('"')
        try:
            parsed_value = json.loads(value)
        except Exception:
            # attempt numeric conversion
            try:
                parsed_value = float(value)
                if parsed_value.is_integer():
                    parsed_value = int(parsed_value)
            except Exception:
                parsed_value = value
        result[key] = parsed_value
    return result or None


def _current_health_snapshot() -> dict[str, Any]:
    up, last_probe_ms, _ = sample_once()
    agg = aggregate()
    up, agg, fault = apply_fault_overlay(up, agg)

    error_rate = float(agg.get("error_rate", 0.0) or 0.0)
    p95_ms = float(agg.get("p95_ms", 0.0) or 0.0)

    degraded = (not up) or (error_rate > ERROR_RATE_MAX) or (p95_ms > P95_MS_MAX)

    if not up:
        status = "Down"
    elif degraded:
        status = "Degraded"
    else:
        status = "Healthy"

    overlay_status = fault.get("overlay", {}).get("status") if fault.get("active") else None
    if overlay_status:
        status = str(overlay_status)

    return {
        "status": status,
        "healthy": (up and not degraded),
        "up": up,
        "last_probe_ms": last_probe_ms,
        "error_rate": round(error_rate, 3),
        "p95_ms": int(p95_ms),
        "thresholds": {"error_rate_max": ERROR_RATE_MAX, "p95_ms_max": P95_MS_MAX},
        "fault_state": fault,
    }


def _format_health_summary(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status", "Unknown")
    error_rate = snapshot.get("error_rate")
    p95_ms = snapshot.get("p95_ms")
    pieces = [f"Status: {status}"]
    if error_rate is not None:
        pieces.append(f"Errors: {float(error_rate) * 100:.1f}%")
    if p95_ms is not None:
        pieces.append(f"p95 latency: {p95_ms}ms")
    if snapshot.get("fault_state", {}).get("active"):
        label = snapshot["fault_state"].get("profile_label") or "fault injected"
        pieces.append(f"Active fault: {label}")
    return " | ".join(pieces)


def _explain_latency(snapshot: dict[str, Any]) -> str:
    p95_ms = float(snapshot.get("p95_ms") or 0.0)
    error_rate = float(snapshot.get("error_rate") or 0.0)
    if p95_ms <= P95_MS_MAX and error_rate <= ERROR_RATE_MAX:
        return "Latency is within normal thresholds right now."
    if p95_ms > P95_MS_MAX * 2:
        return (
            f"Latency is extremely high ({p95_ms}ms p95). Consider scaling out or checking upstream dependencies."
        )
    if p95_ms > P95_MS_MAX:
        return (
            f"Latency p95 is {p95_ms}ms, exceeding the {P95_MS_MAX}ms threshold. The assistant already triggered a restart to mitigate."
        )
    if error_rate > ERROR_RATE_MAX:
        return (
            f"Latency is tied to error bursts (error rate {error_rate*100:.1f}%). Restart or rollback may help."
        )
    return "Latency spike cause is unclear. Review service metrics and logs for more detail."


def _dispatch_restart(parameters: dict[str, Any]) -> dict[str, Any]:
    service = parameters.get("service") or DEFAULT_SERVICE
    environment = parameters.get("environment") or DEFAULT_ENV
    namespace = parameters.get("namespace") or DEFAULT_NAMESPACE
    region = parameters.get("region") or DEFAULT_REGION
    host_port = parameters.get("host_port") or DEFAULT_HOST_PORT
    container_port = parameters.get("container_port") or DEFAULT_CONTAINER_PORT

    incident_identifier = parameters.get("incident_id") or f"assistant-{int(time.time())}"

    request = models.ActionRequest(
        incident_id=incident_identifier,
        workflow="restart",
        parameters={
            "service": service,
            "environment": environment,
            "namespace": namespace,
            "region": region,
            "host_port": host_port,
            "container_port": container_port,
        },
    )

    response = actions.trigger_workflow("restart", request)
    ok = response.status in {"queued", "triggered"}

    return {
        "ok": ok,
        "message": response.message,
        "workflow_status": response.status,
        "run_url": response.run_url,
        "details": response.details,
        "parameters": request.parameters,
    }


def _build_context(intent: str, parameters: dict[str, Any]) -> dict[str, Any]:
    service = parameters.get("service") or DEFAULT_SERVICE

    context: dict[str, Any] = {
        "service": service,
        "knowledge_base": MOCK_KNOWLEDGE_BASE.get("services", {}).get(service, {}),
        "incidents": MOCK_KNOWLEDGE_BASE.get("incidents", []),
        "metrics": MOCK_KNOWLEDGE_BASE.get("metrics", {}),
        "parameters": parameters,
    }

    if intent in {"check_health", "explain_latency", "restart_service"}:
        context["health_snapshot"] = _current_health_snapshot()

    return context


def _generate_response_text(
    original_message: str,
    intent: str,
    acknowledgement: str | None,
    confidence: float,
    context: dict[str, Any],
) -> str:
    fallback_messages = {
        "check_health": _format_health_summary(context.get("health_snapshot", {})),
        "explain_latency": _explain_latency(context.get("health_snapshot", {})),
        "restart_service": "Triggered restart workflow.",
        "greeting": "Hi there! I'm here to help monitor and heal your services.",
        "fallback": "I'm not sure how to help with that yet."
    }

    if not rca.configure_client():
        return acknowledgement or fallback_messages.get(intent, fallback_messages["fallback"])

    try:
        model_name = utils.get_env("GEMINI_MODEL", "gemini-2.0-flash")
        model = genai.GenerativeModel(model_name)
        prompt = (
            "You are AutoMedic's SRE copilot. Given the following knowledge base and the user "
            "instruction, craft a concise, helpful response (under 4 sentences). If you triggered "
            "an action, mention it. If data is missing, say so politely.\n"
            f"Knowledge base:\n```json\n{json.dumps(context, indent=2)}\n```\n"
            f"Intent: {intent}\n"
            f"Confidence: {confidence:.2f}\n"
            f"User instruction: {original_message}\n"
            "Response:"
        )
        response = model.generate_content(prompt)
        text = _extract_first_text(response)
        if text:
            return text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Assistant response generation failed: %s", exc)
        context.setdefault("debug", {})
        context["debug"]["response_error"] = str(exc)

    return acknowledgement or fallback_messages.get(intent, fallback_messages["fallback"])


def _extract_first_text(response: Any) -> str | None:
    if hasattr(response, "text") and isinstance(response.text, str):
        return response.text
    if hasattr(response, "candidates") and response.candidates:
        candidate = response.candidates[0]
        parts = getattr(candidate, "content", None)
        if parts and hasattr(parts, "parts"):
            return "".join(
                part.text for part in parts.parts if hasattr(part, "text") and isinstance(part.text, str)
            )
    return None
