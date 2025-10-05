"""FastAPI application entrypoint for HealOps backend services."""

import os
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables from .env file
load_dotenv()

from . import actions, models, predict_service, predictive, rca, watcher
from .health_probe import (
    aggregate,
    apply_fault_overlay,
    clear_fault,
    inject_fault,
    sample_once,
)

app = FastAPI(title="HealOps Backend")

# Add CORS middleware to allow frontend connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# thresholds (tunable via env)
ERROR_RATE_MAX = float(os.getenv("ERROR_RATE_MAX", "0.05"))  # 5%
P95_MS_MAX = int(os.getenv("P95_MS_MAX", "1000"))            # 1s


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize background watchers and any required resources."""
    watcher.start_monitoring()


@app.get("/health")
def health() -> dict[str, object]:
    """Expose synthetic health data derived from the rolling probe window."""
    up, last_probe_ms, _ = sample_once()
    agg = aggregate()
    up, agg, fault = apply_fault_overlay(up, agg)

    error_rate = float(agg.get("error_rate", 0.0) or 0.0)
    p95_ms = float(agg.get("p95_ms", 0.0) or 0.0)

    predictive.record_snapshot(
        {
            "error_rate": error_rate,
            "p95_ms": p95_ms,
            "cpu": agg.get("cpu"),
            "mem": agg.get("mem"),
        }
    )

    degraded = (not up) or (error_rate > ERROR_RATE_MAX) or (p95_ms > P95_MS_MAX)

    if not up:
        status = "Down ⛔"
    elif degraded:
        status = "Degraded ❌"
    else:
        status = "Healthy ✅"

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
        "fault_active": fault.get("active"),
        "fault_profile": fault.get("profile"),
        "fault_profile_label": fault.get("profile_label"),
        "fault_remaining_s": fault.get("remaining_seconds"),
        "fault_starts_at": fault.get("started_at"),
        "fault_ends_at": fault.get("ends_at"),
    }


@app.get("/health/debug")
def health_debug() -> dict[str, object]:
    """Expose raw probe metrics to help with demo debugging."""
    up, last_probe_ms, ok = sample_once()
    agg = aggregate()
    return {"up": up, "last_probe_ms": last_probe_ms, "last_probe_ok": ok, **agg}


@app.post("/incidents")
async def analyze_incident(incident: models.Incident) -> models.RCAResult:
    """Run root-cause analysis on a reported incident and return the findings."""
    findings = rca.run_root_cause_analysis(incident)
    return findings


@app.post("/actions/restart")
async def trigger_restart(action: models.ActionRequest) -> models.ActionResponse:
    """Trigger a restart workflow for the affected service."""
    outcome = actions.trigger_workflow("restart", action)
    return outcome


@app.post("/actions/rollback")
async def trigger_rollback(action: models.ActionRequest) -> models.ActionResponse:
    """Trigger a rollback workflow for the affected service."""
    outcome = actions.trigger_workflow("rollback", action)
    return outcome


@app.post("/actions/scale-up")
async def trigger_scale_up(action: models.ActionRequest) -> models.ActionResponse:
    """Trigger a scale-up workflow for the affected service."""
    outcome = actions.trigger_workflow("scale_up", action)
    return outcome


@app.post("/faults/inject")
def inject_fault_endpoint(request: models.FaultInjectionRequest) -> models.FaultInjectionState:
    """Simulate a degraded health state for demo purposes."""
    try:
        state = inject_fault(request.profile, request.duration_seconds)
    except ValueError as exc:  # noqa: PERF203 - explicit for FastAPI HTTP error
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return models.FaultInjectionState(**state)


@app.post("/faults/reset")
def reset_fault_endpoint() -> models.FaultInjectionState:
    """Clear any active fault injection."""
    state = clear_fault()
    return models.FaultInjectionState(**state)


@app.get("/predict", response_model=list[models.PredictiveForecast])
def predict_metrics() -> list[models.PredictiveForecast]:
    """Return forecasts for metrics that are likely to breach thresholds soon."""

    forecasts = predict_service.predict_upcoming()
    return [models.PredictiveForecast(**forecast.to_dict()) for forecast in forecasts]
