"""Service utilities that orchestrate predictive forecasts."""

from __future__ import annotations

import os
from typing import List

from . import predictive


ERR_MAX = float(os.getenv("ERROR_RATE_MAX", "0.05"))
P95_MAX = float(os.getenv("P95_MS_MAX", "1000"))
CPU_MAX = float(os.getenv("CPU_MAX", "85"))
MEM_MAX = float(os.getenv("MEM_MAX", "90"))

PREDICTIVE_ENABLED = os.getenv("PREDICTIVE_ENABLED", "true").lower() not in {"0", "false", "no"}
PREDICT_HORIZON_SEC = int(os.getenv("PREDICT_HORIZON_SEC", "600"))
PREDICT_MIN_CONF = float(os.getenv("PREDICT_MIN_CONF", "0.1"))


SAFE_METRICS = (
    ("error_rate", ERR_MAX, "up"),
    ("p95_ms", P95_MAX, "up"),
    ("cpu", CPU_MAX, "up"),
    ("mem", MEM_MAX, "up"),
)


def predict_upcoming() -> List[predictive.Forecast]:
    """Return forecasts that are likely to breach thresholds soon."""

    if not PREDICTIVE_ENABLED:
        return []

    forecasts: list[predictive.Forecast] = []
    for metric, threshold, direction in SAFE_METRICS:
        forecast = predictive.forecast_metric(metric, PREDICT_HORIZON_SEC, threshold, direction=direction)
        if not forecast:
            continue
        if not forecast.crosses_threshold:
            continue
        if forecast.confidence < PREDICT_MIN_CONF:
            continue
        forecasts.append(forecast)

    return forecasts
