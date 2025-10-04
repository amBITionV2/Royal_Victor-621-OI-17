"""Tests for the Gemini RCA integration logic."""

from healops.backend import models, rca


def test_run_root_cause_analysis_requires_api_key(monkeypatch):
    incident = models.Incident(
        id="123",
        service="payments",
        severity="high",
        description="Service outage",
    )

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    try:
        rca.run_root_cause_analysis(incident)
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError when API key missing")
