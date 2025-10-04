"""FastAPI application entrypoint for HealOps backend services."""

import os
from dotenv import load_dotenv

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables from .env file
load_dotenv()

from . import rca, actions, watcher, models
from .health_probe import aggregate, sample_once

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
    degraded = (not up) or (agg["error_rate"] > ERROR_RATE_MAX) or (agg["p95_ms"] > P95_MS_MAX)
    status = "Healthy ✅" if (up and not degraded) else ("Degraded ❌" if up else "Down ⛔")

    return {
        "status": status,
        "healthy": (up and not degraded),
        "up": up,
        "last_probe_ms": last_probe_ms,
        **agg,
        "thresholds": {"error_rate_max": ERROR_RATE_MAX, "p95_ms_max": P95_MS_MAX},
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
