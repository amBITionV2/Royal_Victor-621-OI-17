import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import requests
import streamlit as st


st.set_page_config(page_title="HealOps", layout="wide")

BACKEND_URL = os.getenv("HEALOPS_BACKEND_URL", "http://localhost:4000").rstrip("/")
GITHUB_REPO = os.getenv("GITHUB_REPO", "youruser/healops-backend")
GITHUB_TOKEN = os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN")
GITHUB_ACTIONS_URL = f"https://github.com/{GITHUB_REPO}/actions"

ACTION_ENDPOINTS = {
    "restart": "restart",
    "rollback": "rollback",
    "scale_up": "scale-up",
}

WORKFLOW_FILES = {
    "restart": os.getenv("WF_RESTART", "restart.yml"),
    "rollback": os.getenv("WF_ROLLBACK", "rollback.yml"),
    "scale_up": os.getenv("WF_SCALEUP", "scale_up.yml"),
    "clear_cache": os.getenv("WF_CLEAR", "clear_cache.yml"),
}

LOW_RISK_ACTIONS = {"restart", "clear_cache", "scale_up"}

STATUS_BADGES = {
    "queued": ("#FFF3CD", "#856404"),
    "in_progress": ("#E3F2FD", "#0D47A1"),
    "success": ("#E8F5E9", "#1B5E20"),
    "failure": ("#FFEBEE", "#C62828"),
    "error": ("#FFEBEE", "#C62828"),
    "auto": ("#E0F7FA", "#006064"),
}


# --- Session State bootstrap -------------------------------------------------
if "incidents" not in st.session_state:
    st.session_state.incidents: list[dict[str, Any]] = []
if "selected_incident_id" not in st.session_state:
    st.session_state.selected_incident_id: Optional[str] = None
if "activity_feed" not in st.session_state:
    st.session_state.activity_feed: list[dict[str, Any]] = []
if "health_history" not in st.session_state:
    st.session_state.health_history: list[dict[str, Any]] = []


# --- Helpers -----------------------------------------------------------------
def backend_request(method: str, path: str, **kwargs: Any) -> tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    url = f"{BACKEND_URL}{path}"
    timeout = kwargs.pop("timeout", 10)
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        if response.content:
            return True, response.json(), None
        return True, None, None
    except requests.HTTPError as exc:
        detail: Any
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text if exc.response is not None else str(exc)
        return False, None, f"{exc.response.status_code if exc.response else 'HTTP'}: {detail}"
    except requests.RequestException as exc:
        return False, None, str(exc)


def add_activity(message: str, status: Optional[str] = None, details: Optional[str] = None) -> None:
    st.session_state.activity_feed.append(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "status": status,
            "details": details,
        }
    )


def classify_severity(error_rate: Optional[float]) -> str:
    if error_rate is None:
        return "low"
    if error_rate >= 0.2:
        return "critical"
    if error_rate >= 0.1:
        return "high"
    if error_rate >= 0.05:
        return "medium"
    return "low"


def get_health() -> Optional[Dict[str, Any]]:
    ok, data, _ = backend_request("GET", "/health", timeout=5)
    if ok:
        return data or {}
    return None


def record_health_history(sample: Dict[str, Any]) -> None:
    snapshot = {
        "time": datetime.now(),
        "error_rate": float(sample.get("error_rate") or 0.0),
        "p95_ms": float(sample.get("p95_ms") or 0.0),
    }
    st.session_state.health_history.append(snapshot)
    if len(st.session_state.health_history) > 30:
        st.session_state.health_history = st.session_state.health_history[-30:]


def build_incident_payload(incident: dict[str, Any]) -> dict[str, Any]:
    """Build payload for the backend /incidents RCA endpoint."""

    health_snapshot = incident.get("health_snapshot", {})
    error_rate = health_snapshot.get("error_rate")

    metrics = []
    if error_rate is not None:
        metrics.append({"name": "error_rate", "value": float(error_rate)})
    for key in ("p95_ms", "last_probe_ms", "cpu", "mem"):
        value = health_snapshot.get(key)
        if value is not None:
            metrics.append({"name": key, "value": float(value)})

    description = (
        f"Incident {incident['id']} in {incident.get('env', 'env')} - "
        f"{incident.get('metric_breached', 'auto-detected anomaly')}"
    )

    payload = {
        "id": incident["id"],
        "service": incident.get("service", "unknown-service"),
        "severity": classify_severity(error_rate),
        "description": description,
        "metrics": metrics,
        "logs": [
            json.dumps(
                {
                    "env": incident.get("env"),
                    "namespace": incident.get("namespace"),
                    "region": incident.get("region"),
                }
            )
        ],
        "reported_at": incident["opened_at"].isoformat(),
    }
    return payload


def extract_recommended_action(rca: Dict[str, Any]) -> Optional[str]:
    metadata = rca.get("metadata") or {}
    for key in ("recommended_action", "suggested_action", "action"):
        value = metadata.get(key)
        if isinstance(value, str):
            normalized = value.lower().replace("-", "_").strip()
            if normalized in ACTION_ENDPOINTS:
                return normalized

    steps = rca.get("remediation_steps") or []
    if isinstance(steps, list):
        joined = " ".join(str(step) for step in steps).lower()
        for candidate in ACTION_ENDPOINTS:
            if candidate.replace("_", " ") in joined:
                return candidate
    return None


def normalize_rca_response(response: Dict[str, Any]) -> Dict[str, Any]:
    probable_causes = response.get("probable_causes") or []
    root_cause = probable_causes[0] if probable_causes else response.get("summary")

    normalized = {
        "summary": response.get("summary"),
        "probable_causes": probable_causes,
        "remediation_steps": response.get("remediation_steps") or [],
        "confidence": response.get("confidence", 0.0),
        "recommended_action": extract_recommended_action(response) or response.get("recommended_action"),
        "metadata": response.get("metadata") or {},
        "root_cause": root_cause,
        "raw": response,
    }
    return normalized


def run_rca(incident: dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = build_incident_payload(incident)
    ok, data, error = backend_request("POST", "/incidents", json=payload, timeout=20)
    if ok and isinstance(data, dict):
        return normalize_rca_response(data)

    add_activity(f"RCA failed for {incident['id']}", "error", error)
    st.error(f"Failed to run RCA: {error}")
    return None


def trigger_backend_workflow(action: str, incident: dict[str, Any], parameters: dict[str, Any]) -> tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    endpoint = ACTION_ENDPOINTS.get(action)
    if not endpoint:
        return False, None, f"Action '{action}' not supported by backend"

    payload = {
        "incident_id": incident["id"],
        "workflow": action,
        "parameters": parameters,
    }
    return backend_request("POST", f"/actions/{endpoint}", json=payload, timeout=20)


def fetch_latest_run(workflow: str) -> Optional[Dict[str, Any]]:
    workflow_file = WORKFLOW_FILES.get(workflow)
    if not workflow_file or not GITHUB_TOKEN:
        return None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/runs?per_page=1"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        runs = response.json().get("workflow_runs") or []
        return runs[0] if runs else None
    except requests.RequestException:
        return None


def render_status_chip(label: str, variant: str) -> None:
    bg, fg = STATUS_BADGES.get(variant, ("#E0E0E0", "#424242"))
    st.markdown(
        f"<span style='display:inline-block;padding:0.2rem 0.65rem;border-radius:999px;"
        f"font-size:0.85rem;font-weight:600;background:{bg};color:{fg};'>{label}</span>",
        unsafe_allow_html=True,
    )


def render_status_pill(healthy: bool, status_text: str) -> None:
    bg = "#E8F5E9" if healthy else "#FFEBEE"
    fg = "#1B5E20" if healthy else "#C62828"
    icon = "✅" if healthy else "❌"
    st.markdown(
        f"<div style='display:inline-block;padding:0.45rem 0.9rem;border-radius:999px;"
        f"font-size:1rem;font-weight:600;background:{bg};color:{fg};'>{icon} {status_text}</div>",
        unsafe_allow_html=True,
    )


def get_incident_by_id(incident_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not incident_id:
        return None
    for incident in st.session_state.incidents:
        if incident["id"] == incident_id:
            return incident
    return None


def ensure_incident_defaults(incident: dict[str, Any]) -> None:
    incident.setdefault("workflow_dispatched", False)
    incident.setdefault("workflow_status", None)
    incident.setdefault("notes", [])


# --- Layout: Header ----------------------------------------------------------
header_col1, header_col2, header_col3 = st.columns([2, 1, 1])

with header_col1:
    st.title("🩹 HealOps — self-healing demo")

with header_col2:
    selected_env = st.selectbox("Environment", ["prod", "stage"], key="env_selector")

with header_col3:
    st.markdown(f"[🔗 Last GitHub Action run]({GITHUB_ACTIONS_URL})")


st.divider()


# --- Sidebar -----------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    service = st.text_input("Service", "demo-api")
    namespace = st.text_input("Namespace", "default")
    region = st.text_input("Region", "ap-south-1")
    host_port = st.text_input("Host port", "8080")
    container_port = st.text_input("Container port", "80")

    auto_low_risk = st.toggle("Auto-run low-risk fixes", True)
    conf_thresh = st.slider("Min confidence for auto-run", 0.0, 1.0, 0.7, 0.05)

    st.divider()
    st.header("📋 Activity Feed")
    for entry in reversed(st.session_state.activity_feed[-12:]):
        st.write(f"**{entry['time']}** — {entry['message']}")
        detail = entry.get("details")
        if detail:
            if entry.get("status") == "success":
                st.success(detail)
            elif entry.get("status") == "error":
                st.error(detail)
            else:
                st.info(detail)


# --- Live Health Panel -------------------------------------------------------
st.subheader("Live Health")
health = get_health()

if health is None:
    st.warning("Unable to reach backend /health endpoint. Is the service running?")
else:
    record_health_history(health)
    col_status, col_metrics = st.columns([1.4, 2.6])

    with col_status:
        render_status_pill(bool(health.get("healthy")), health.get("status", "Unknown"))
        st.caption(
            f"Last probe: {int(health.get('last_probe_ms', 0))}ms | Error rate: {health.get('error_rate', 'n/a')}"
        )

    with col_metrics:
        mcol1, mcol2, mcol3, mcol4 = st.columns(4)
        error_rate = health.get("error_rate")
        error_display = f"{float(error_rate) * 100:.1f}%" if error_rate is not None else "n/a"
        mcol1.metric("Error rate", error_display)
        mcol2.metric("p95 (ms)", health.get("p95_ms", "n/a"))
        cpu_value = health.get("cpu")
        mcol3.metric("CPU %", f"{float(cpu_value):.0f}%" if cpu_value is not None else "n/a")
        mem_value = health.get("mem")
        mcol4.metric("Mem %", f"{float(mem_value):.0f}%" if mem_value is not None else "n/a")

    if len(st.session_state.health_history) > 1:
        chart_df = pd.DataFrame(
            {
                "error_rate": [h["error_rate"] * 100 for h in st.session_state.health_history],
                "p95_ms": [h["p95_ms"] for h in st.session_state.health_history],
            }
        )
        chart_df.index = range(-len(chart_df) + 1, 1)
        st.line_chart(chart_df, height=180)


st.divider()


# --- Incident Management -----------------------------------------------------
st.subheader("Incidents")

def create_incident() -> None:
    if not health:
        st.error("Cannot open an incident without health telemetry.")
        return

    incident_id = f"INC-{len(st.session_state.incidents) + 1:03d}"
    thresholds = (health or {}).get("thresholds", {})
    breach = "Manual trigger"
    error_rate_val = health.get("error_rate") or 0.0
    p95_val = health.get("p95_ms") or 0.0
    cpu_val = health.get("cpu") or 0.0
    threshold_error = thresholds.get("error_rate_max", 0.05)
    threshold_p95 = thresholds.get("p95_ms_max", 1000)
    if error_rate_val > threshold_error:
        breach = f"error_rate {error_rate_val*100:.1f}% > {threshold_error*100:.1f}%"
    elif p95_val > threshold_p95:
        breach = f"p95 {p95_val}ms > {threshold_p95}ms"
    elif cpu_val and cpu_val > 85:
        breach = f"cpu {cpu_val}% > 85%"

    incident = {
        "id": incident_id,
        "opened_at": datetime.now(),
        "metric_breached": breach,
        "status": "open",
        "health_snapshot": health,
        "service": service,
        "namespace": namespace,
        "region": region,
        "env": selected_env,
        "host_port": host_port,
        "container_port": container_port,
    }
    st.session_state.incidents.append(incident)
    add_activity(f"Incident {incident_id} opened", "info", breach)


col_incident_btn, _ = st.columns([1, 3])
with col_incident_btn:
    if st.button("🚨 Create Incident", use_container_width=True):
        create_incident()


if st.session_state.incidents:
    table_container = st.container(border=True)
    with table_container:
        header_cols = st.columns([1, 1.8, 2.2, 1.2, 0.8])
        header_cols[0].markdown("**ID**")
        header_cols[1].markdown("**Opened at**")
        header_cols[2].markdown("**Metric breached**")
        header_cols[3].markdown("**Status**")
        header_cols[4].markdown("**Action**")

        for incident in st.session_state.incidents:
            ensure_incident_defaults(incident)
            row_cols = st.columns([1, 1.8, 2.2, 1.2, 0.8])
            row_cols[0].write(incident["id"])
            row_cols[1].write(incident["opened_at"].strftime("%H:%M:%S"))
            row_cols[2].write(incident["metric_breached"])
            status_icon = {
                "open": "🔴",
                "diagnosed": "🟡",
                "resolving": "🟠",
                "resolved": "🟢",
            }.get(incident.get("status"), "⚪")
            row_cols[3].write(f"{status_icon} {incident.get('status', 'unknown')}")
            if row_cols[4].button("View", key=f"view_{incident['id']}"):
                st.session_state.selected_incident_id = incident["id"]

else:
    st.info("No incidents yet. Use 'Create Incident' to open one.")


selected_incident = get_incident_by_id(st.session_state.selected_incident_id)

st.divider()


# --- RCA Panel ---------------------------------------------------------------
if selected_incident:
    st.subheader("Root Cause Analysis (Gemini)")

    action_cols = st.columns([1, 3])
    if action_cols[0].button("Run RCA", key=f"rca_{selected_incident['id']}"):
        with st.spinner("Running Gemini RCA…"):
            rca_result = run_rca(selected_incident)
        if rca_result:
            selected_incident["rca"] = rca_result
            selected_incident["status"] = "diagnosed"
            add_activity(
                f"RCA complete for {selected_incident['id']}",
                "success",
                f"Confidence {float(rca_result.get('confidence', 0.0)):.2f}",
            )

    rca_payload = selected_incident.get("rca")
    if rca_payload:
        summary_col, controls_col = st.columns([2, 1])

        with summary_col:
            human_line = (
                f"Likely cause: {rca_payload.get('root_cause', 'unknown')} "
                f"({float(rca_payload.get('confidence', 0.0)):.2f}) → Suggest: "
                f"{rca_payload.get('recommended_action', 'n/a')}"
            )
            st.write(human_line)
            st.json(
                {
                    "summary": rca_payload.get("summary"),
                    "root_cause": rca_payload.get("root_cause"),
                    "probable_causes": rca_payload.get("probable_causes"),
                    "confidence": rca_payload.get("confidence"),
                    "recommended_action": rca_payload.get("recommended_action"),
                    "remediation_steps": rca_payload.get("remediation_steps"),
                }
            )

        with controls_col:
            confidence_value = float(rca_payload.get("confidence", 0.0) or 0.0)
            recommended_action = rca_payload.get("recommended_action")
            workflow_state = selected_incident.get("workflow_status") or {}

            if recommended_action:
                st.write(f"**Recommendation:** `{recommended_action}`")
            st.write(f"**Confidence:** {confidence_value:.2f}")

            if recommended_action in LOW_RISK_ACTIONS and recommended_action in ACTION_ENDPOINTS:
                render_status_chip("Low-risk", "auto")
            if (
                recommended_action in LOW_RISK_ACTIONS
                and recommended_action in ACTION_ENDPOINTS
                and confidence_value >= conf_thresh
                and auto_low_risk
            ):
                st.caption("Low-risk fix meets threshold. Approval required to execute.")

            if workflow_state:
                render_status_chip(
                    workflow_state.get("label", workflow_state.get("state", "queued")),
                    workflow_state.get("state", "queued"),
                )
                run_link = workflow_state.get("run_url")
                if run_link:
                    st.markdown(f"[View run ↗]({run_link})")

            incident_inputs = {
                "service": selected_incident.get("service") or service,
                "namespace": selected_incident.get("namespace") or namespace,
                "region": selected_incident.get("region") or region,
                "environment": selected_incident.get("env") or selected_env,
                "host_port": selected_incident.get("host_port") or host_port,
                "container_port": selected_incident.get("container_port") or container_port,
            }

            action_options = list(ACTION_ENDPOINTS.keys())
            default_index = action_options.index(recommended_action) if recommended_action in action_options else 0
            selected_action = st.selectbox(
                "Select action",
                action_options,
                index=default_index,
                key=f"action_select_{selected_incident['id']}",
                disabled=selected_incident.get("workflow_dispatched"),
            )

            if st.button(
                "Approve & Execute",
                key=f"dispatch_{selected_incident['id']}",
                disabled=selected_incident.get("workflow_dispatched"),
            ):
                ok, data, error = trigger_backend_workflow(selected_action, selected_incident, incident_inputs)
                if ok:
                    selected_incident["workflow_dispatched"] = True
                    selected_incident["status"] = "resolving"
                    selected_incident["dispatched_action"] = selected_action
                    selected_incident["workflow_status"] = {
                        "state": "queued",
                        "label": (data or {}).get("message", "Workflow queued"),
                        "run_url": (data or {}).get("run_url") or GITHUB_ACTIONS_URL,
                    }
                    add_activity(
                        f"Workflow dispatched for {selected_incident['id']}",
                        "success",
                        (data or {}).get("message"),
                    )
                else:
                    add_activity(
                        f"Workflow dispatch failed for {selected_incident['id']}",
                        "error",
                        error,
                    )
                    st.error(f"Failed to dispatch: {error}")

            if selected_incident.get("workflow_dispatched"):
                action_for_status = selected_incident.get("dispatched_action") or recommended_action
                if action_for_status:
                    run_info = fetch_latest_run(action_for_status)
                    if run_info:
                        conclusion = run_info.get("conclusion")
                        status = run_info.get("status") or "queued"
                        state = "queued"
                        label = status
                        if status == "queued":
                            state = "queued"
                            label = "Queued"
                        elif status == "in_progress":
                            state = "in_progress"
                            label = "In progress"
                        elif status == "completed":
                            if conclusion == "success":
                                state = "success"
                                label = "Success"
                                selected_incident["status"] = "resolving"
                            else:
                                state = "failure"
                                label = conclusion or "Failed"
                        selected_incident["workflow_status"] = {
                            "state": state,
                            "label": label,
                            "run_url": run_info.get("html_url") or GITHUB_ACTIONS_URL,
                        }


    st.divider()

    # --- Verification & Notes -------------------------------------------------
    st.subheader("Verification & Notes")
    verify_col, note_col, close_col = st.columns([1.2, 2, 1])

    if verify_col.button("Verify recovery", key=f"verify_{selected_incident['id']}"):
        with st.spinner("Checking health for 20 seconds..."):
            recovered = False
            start = time.time()
            for _ in range(20):
                time.sleep(1)
                refreshed = get_health()
                if not refreshed:
                    continue
                error_rate_val = float(refreshed.get("error_rate") or 0.0)
                p95_val = float(refreshed.get("p95_ms") or 0.0)
                cpu_val = float(refreshed.get("cpu") or 0.0)
                thresholds = refreshed.get("thresholds", {})
                if (
                    refreshed.get("healthy")
                    or (error_rate_val < float(thresholds.get("error_rate_max", 0.05))
                        and p95_val < float(thresholds.get("p95_ms_max", 1000))
                        and cpu_val < 85)
                ):
                    recovered = True
                    recovery_time = int(time.time() - start)
                    selected_incident["status"] = "resolved"
                    selected_incident["recovery_time"] = recovery_time
                    add_activity(
                        f"Incident {selected_incident['id']} recovered",
                        "success",
                        f"Recovered in {recovery_time}s",
                    )
                    break
        if recovered:
            st.success(f"Recovered in {selected_incident.get('recovery_time', 0)}s")
        else:
            st.error("Still degraded after verification window")
            add_activity(
                f"Recovery failed for {selected_incident['id']}",
                "error",
                "Still degraded",
            )

    note = note_col.text_area(
        "Add a note", key=f"note_input_{selected_incident['id']}", placeholder="Runbook snippets, follow-ups..."
    )
    if note and note_col.button("Save note", key=f"save_note_{selected_incident['id']}"):
        selected_incident.setdefault("notes", []).append({"time": datetime.now(), "note": note})
        add_activity("Note added", "info", note[:80])
        note_col.success("Note saved")

    if selected_incident.get("status") == "resolved":
        if close_col.button("Close incident", key=f"close_{selected_incident['id']}"):
            add_activity(f"Incident {selected_incident['id']} closed", "success")
            st.session_state.selected_incident_id = None
            st.session_state.incidents = [
                inc for inc in st.session_state.incidents if inc["id"] != selected_incident["id"]
            ]
            st.success("Incident closed")
            st.rerun()

    if selected_incident.get("notes"):
        st.markdown("**Notes**")
        for entry in reversed(selected_incident["notes"]):
            st.write(f"{entry['time'].strftime('%H:%M:%S')} — {entry['note']}")


# --- Activity feed fallback when no incident selected -----------------------
else:
    st.info("Select an incident to view Gemini RCA and remediation controls.")
