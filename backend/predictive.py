"""Predictive analytics helpers for near-term metric forecasting."""

from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, Optional

import numpy as np

HISTORY_MAX = int(float(240))  # keep ~2 hours at 30s cadence (adjustable via env later)
MIN_POINTS = 12  # require ~6 minutes of data to reduce noise


class MetricSeries(collections.deque[tuple[float, float]]):
    """Deque that remembers its metric name for debugging and reporting."""

    def __init__(self, name: str, maxlen: int = HISTORY_MAX) -> None:  # noqa: D401
        super().__init__(maxlen=maxlen)
        self.name = name


METRICS: Dict[str, MetricSeries] = {
    "error_rate": MetricSeries("error_rate"),
    "p95_ms": MetricSeries("p95_ms"),
    "cpu": MetricSeries("cpu"),
    "mem": MetricSeries("mem"),
}


@dataclass(slots=True)
class Forecast:
    """Simple forecast describing a potential threshold breach."""

    metric: str
    horizon_sec: int
    predicted_value: float
    crosses_threshold: bool
    time_to_cross_sec: Optional[int]
    confidence: float
    slope: float
    baseline: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def add_point(name: str, value: float, ts: Optional[float] = None) -> None:
    """Record a new observation into the rolling window for a metric."""

    if name not in METRICS:
        METRICS[name] = MetricSeries(name)

    if value is None or math.isnan(value):
        return

    series = METRICS[name]
    ts = ts if ts is not None else time.time()
    series.append((float(ts), float(value)))


def record_snapshot(values: Dict[str, float], ts: Optional[float] = None) -> None:
    """Convenience wrapper to ingest multiple metric values at once."""

    for key, raw in values.items():
        if raw is None:
            continue
        try:
            add_point(key, float(raw), ts)
        except (TypeError, ValueError):
            continue


def ewma(values: Iterable[float], alpha: float = 0.2) -> float:
    """Compute an exponentially-weighted moving average for context."""

    iterator = iter(values)
    try:
        acc = float(next(iterator))
    except StopIteration:
        return 0.0

    for value in iterator:
        acc = alpha * float(value) + (1 - alpha) * acc
    return acc


def _linear_forecast(
    series: MetricSeries,
    horizon_sec: int,
    threshold: float,
    direction: str,
) -> Optional[Forecast]:
    """Fit a weighted linear trend and project ahead to detect breaches."""

    if len(series) < MIN_POINTS:
        return None

    xs = np.array([point[0] for point in series], dtype=float)
    ys = np.array([point[1] for point in series], dtype=float)

    t0 = xs[0]
    xs = xs - t0

    # Down-weight abrupt last-point spikes to reduce false positives.
    weights = np.ones_like(xs)
    if len(ys) > 3:
        std = np.std(ys[:-1]) if len(ys) > 4 else np.std(ys)
        if std > 0 and abs(ys[-1] - ys[-2]) > 3 * std:
            weights[-1] = 0.4

    coeffs = np.polyfit(xs, ys, 1, w=weights)
    slope, intercept = float(coeffs[0]), float(coeffs[1])

    future_t = xs[-1] + horizon_sec
    predicted = slope * future_t + intercept

    time_to_cross: Optional[int] = None
    crosses = False

    if direction == "up" and slope > 0:
        t_cross = (threshold - intercept) / (slope or np.finfo(float).eps)
        if t_cross >= xs[-1]:
            delta = t_cross - xs[-1]
            if delta <= horizon_sec:
                time_to_cross = int(max(0, round(delta)))
                crosses = True
    elif direction == "down" and slope < 0:
        t_cross = (threshold - intercept) / (slope or np.finfo(float).eps)
        if t_cross >= xs[-1]:
            delta = t_cross - xs[-1]
            if delta <= horizon_sec:
                time_to_cross = int(max(0, round(delta)))
                crosses = True

    noise = np.std(ys)
    strength = abs(slope) * (horizon_sec / max(1.0, noise + 1e-6))
    confidence = max(0.0, min(1.0, 0.25 + 0.18 * strength))

    baseline = ewma(ys)

    return Forecast(
        metric=series.name,
        horizon_sec=horizon_sec,
        predicted_value=float(predicted),
        crosses_threshold=crosses,
        time_to_cross_sec=time_to_cross,
        confidence=float(confidence),
        slope=float(slope),
        baseline=float(baseline),
    )


def _safe_threshold(threshold: Optional[float]) -> Optional[float]:
    if threshold is None or math.isnan(threshold):
        return None
    return float(threshold)


def forecast_metric(
    name: str,
    horizon_sec: int,
    threshold: Optional[float],
    direction: str = "up",
) -> Optional[Forecast]:
    """Produce a forecast for a specific metric if possible."""

    series = METRICS.get(name)
    threshold_value = _safe_threshold(threshold)
    if series is None or threshold_value is None:
        return None

    return _linear_forecast(series, horizon_sec, threshold_value, direction)


def reset() -> None:
    """Clear all stored series (useful for tests)."""

    for series in METRICS.values():
        series.clear()
