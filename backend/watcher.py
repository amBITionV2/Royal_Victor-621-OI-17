"""Background health monitoring routines for HealOps."""

from __future__ import annotations

import threading
import time

from . import models, utils

_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start_monitoring(poll_interval: float = 15.0) -> None:
    """Launch the background monitor thread if not already started."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return

    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(poll_interval,),
        name="healops-monitor",
        daemon=True,
    )
    _monitor_thread.start()
    utils.logger.info("Background monitoring started with interval %.2fs", poll_interval)


def stop_monitoring() -> None:
    """Signal the monitoring thread to stop."""
    _stop_event.set()


def _monitor_loop(poll_interval: float) -> None:
    """Continuously poll service health metrics and trigger RCA if needed."""
    while not _stop_event.is_set():
        try:
            incidents = _poll_health()
            for incident in incidents:
                utils.logger.info("Detected incident: %s", incident.id)
        except Exception as exc:  # noqa: BLE001
            utils.logger.exception("Watcher encountered an error: %s", exc)
        finally:
            time.sleep(poll_interval)


def _poll_health() -> list[models.Incident]:
    """Placeholder for logic that inspects metrics/logs and returns incidents."""
    utils.logger.debug("Polling for incidents")
    return []
