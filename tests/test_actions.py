"""Tests for the GitHub Actions workflow triggering logic."""

from unittest import mock

import requests

from healops.backend import actions, models


def test_trigger_workflow_missing_configuration(monkeypatch):
    request = models.ActionRequest(
        incident_id="INC-1",
        workflow="restart",
    )

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    try:
        actions.trigger_workflow("restart", request)
    except RuntimeError as exc:
        assert "GITHUB_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError when configuration missing")


def test_trigger_workflow_success(monkeypatch):
    request = models.ActionRequest(
        incident_id="INC-2",
        workflow="restart",
    )

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")

    with mock.patch.object(requests, "post") as mock_post:
        mock_post.return_value.ok = True
        result = actions.trigger_workflow("restart", request)

    assert result.status == "triggered"
    assert result.workflow == "restart"
