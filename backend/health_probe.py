import collections
import os
import statistics
import threading
import time
from datetime import datetime, timezone

import requests

HEALTH_URL = os.getenv("SERVICE_HEALTH_URL", "http://13.203.205.92/health")
PROBE_URL  = os.getenv("SERVICE_PROBE_URL",  "http://13.203.205.92/")  # any cheap GET
TIMEOUT_S  = float(os.getenv("PROBE_TIMEOUT", "2.0"))
WINDOW     = int(os.getenv("PROBE_WINDOW", "20"))  # rolling window size

# rolling window of (latency_ms, ok_bool) samples
samples = collections.deque(maxlen=WINDOW)


FAULT_PROFILES = {
    "latency_spike": {
        "label": "Latency spike",
        "status": "Injected fault: Latency spike",
        "error_rate": 0.35,
        "p95_ms": 2400,
        "up": True,
    },
    "error_burst": {
        "label": "Error burst",
        "status": "Injected fault: Error burst",
        "error_rate": 0.65,
        "p95_ms": 1800,
        "up": True,
    },
    "total_outage": {
        "label": "Total outage",
        "status": "Injected fault: Simulated outage",
        "error_rate": 1.0,
        "p95_ms": 5000,
        "up": False,
    },
}

_fault_lock = threading.Lock()
_fault_profile_key: str | None = None
_fault_active_until: float = 0.0
_fault_started_at: float = 0.0

def sample_once():
    """Ping /health for liveness and PROBE_URL for latency/error; store in window."""
    # 1) liveness via /health
    up = False
    try:
        r = requests.get(HEALTH_URL, timeout=TIMEOUT_S)
        up = (r.status_code == 200)
    except Exception:
        up = False

    # 2) synthetic probe for latency/error
    t0 = time.perf_counter()
    ok = False
    try:
        rp = requests.get(PROBE_URL, timeout=TIMEOUT_S)
        ok = (200 <= rp.status_code < 400)
    except Exception:
        ok = False
    lat_ms = int((time.perf_counter() - t0) * 1000)

    samples.append((lat_ms, ok))
    return up, lat_ms, ok

def aggregate():
    """Compute error_rate and p95 latency over the rolling window."""
    if not samples:
        return {"error_rate": 0.0, "p95_ms": 0}
    lats = [s[0] for s in samples]
    oks  = [s[1] for s in samples]
    error_rate = 1.0 - (sum(oks) / len(oks))
    if len(lats) >= 20:
        # p95 via quantiles (needs >= 20 for stable p95); else use max as a proxy
        p95 = int(statistics.quantiles(lats, n=100)[94])
    else:
        p95 = max(lats)
    return {"error_rate": round(error_rate, 3), "p95_ms": p95}


def _now() -> float:
    return time.time()


def _profile_config(profile: str | None) -> dict[str, float | bool | str] | None:
    return FAULT_PROFILES.get(profile or "")


def inject_fault(profile: str, duration_seconds: int) -> dict[str, object]:
    if profile not in FAULT_PROFILES:
        raise ValueError(f"Unknown fault profile '{profile}'")
    duration = max(5, min(duration_seconds, 900))
    with _fault_lock:
        global _fault_profile_key, _fault_active_until, _fault_started_at
        _fault_profile_key = profile
        _fault_started_at = _now()
        _fault_active_until = _fault_started_at + duration
    return fault_state()


def clear_fault() -> dict[str, object]:
    with _fault_lock:
        global _fault_profile_key, _fault_active_until, _fault_started_at
        _fault_profile_key = None
        _fault_started_at = 0.0
        _fault_active_until = 0.0
    return fault_state()


def fault_state() -> dict[str, object]:
    with _fault_lock:
        remaining = max(0.0, _fault_active_until - _now())
        profile_key = _fault_profile_key if remaining > 0 else None
        started_at = _fault_started_at if profile_key else 0.0
        ends_at = _fault_active_until if profile_key else 0.0
    profile_cfg = _profile_config(profile_key)
    active = bool(profile_key)
    return {
        "active": active,
        "profile": profile_key,
        "profile_label": (profile_cfg or {}).get("label"),
        "remaining_seconds": int(round(remaining)) if active else 0,
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat() if active else None,
        "ends_at": datetime.fromtimestamp(ends_at, tz=timezone.utc).isoformat() if active else None,
        "overlay": profile_cfg or {},
    }


def apply_fault_overlay(up: bool, agg: dict[str, object]) -> tuple[bool, dict[str, object], dict[str, object]]:
    state = fault_state()
    if not state["active"]:
        return up, agg, state

    overlay = state["overlay"]
    error_rate = overlay.get("error_rate")
    p95_ms = overlay.get("p95_ms")
    override_up = overlay.get("up")

    if isinstance(error_rate, (int, float)):
        agg["error_rate"] = max(float(agg.get("error_rate", 0.0)), float(error_rate))
    if isinstance(p95_ms, (int, float)):
        agg["p95_ms"] = max(float(agg.get("p95_ms", 0.0)), float(p95_ms))
    if isinstance(override_up, bool):
        up = override_up

    return up, agg, state
