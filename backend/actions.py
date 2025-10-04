"""GitHub Actions workflow trigger logic for automated remediations."""

from __future__ import annotations

import json
from typing import Any

import requests

from . import models, utils

GITHUB_API = "https://api.github.com"


def trigger_workflow(workflow: str, request: models.ActionRequest) -> models.ActionResponse:
    """Dispatch the appropriate GitHub Actions workflow for the incident."""
    # Try GH_PAT first (your working token), then fallback to GITHUB_TOKEN
    token = utils.get_env("GH_PAT") or utils.get_env("GITHUB_TOKEN")
    repo = utils.get_env("GITHUB_REPOSITORY")
    missing = [name for name, value in (("GH_PAT or GITHUB_TOKEN", token), ("GITHUB_REPOSITORY", repo)) if not value]
    if missing:
        utils.logger.warning("Missing GitHub configuration: %s", ", ".join(missing))
        return models.ActionResponse(
            workflow=workflow,
            status="error",
            message="GitHub credentials not configured",
            details={"missing": missing},
        )

    # Map workflow names to actual workflow files
    workflow_mapping = {
        "restart": "deploy",  # Use deploy.yml for restart actions
        "rollback": "deploy",  # Use deploy.yml for rollback actions  
        "scale_up": "deploy"   # Use deploy.yml for scale_up actions
    }
    actual_workflow = workflow_mapping.get(workflow, workflow)
    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{actual_workflow}.yml/dispatches"
    # Use the same payload format as your working curl command
    payload = {
        "ref": utils.get_env("GITHUB_REF", "main"),
        "inputs": {
            "env": "prod",
            "host_port": "3000", 
            "container_port": "3000"
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        utils.logger.error("Failed to trigger workflow %s: %s", workflow, exc)
        return models.ActionResponse(
            workflow=workflow,
            status="error",
            message=str(exc),
            details={"exception": exc.__class__.__name__},
        )
    if response.ok:
        return models.ActionResponse(
            workflow=workflow,
            status="triggered",
            message="Workflow dispatched successfully",
            run_url=f"https://github.com/{repo}/actions",
            details=payload,
        )

    utils.logger.error("Failed to trigger workflow %s: %s", workflow, response.text)
    return models.ActionResponse(
        workflow=workflow,
        status="error",
        message=response.text,
        details={"status_code": response.status_code},
    )
