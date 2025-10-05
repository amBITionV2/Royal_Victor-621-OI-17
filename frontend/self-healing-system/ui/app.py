import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh


st.set_page_config(page_title="AutoMedic", layout="wide")

AUTO_REFRESH_MS = int(os.getenv("UI_AUTO_REFRESH_MS", "3000"))
if AUTO_REFRESH_MS > 0:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="live_health_refresh")

BACKEND_URL = os.getenv("AUTOMEDIC_BACKEND_URL", "http://localhost:8000").rstrip("/")
GITHUB_REPO = os.getenv("GITHUB_REPO", "youruser/AutoMedic-backend")
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
AUTO_ACTION_MIN_CONF = float(os.getenv("AUTO_ACTION_MIN_CONF", "0.3"))

STATUS_BADGES = {
    "queued": ("#FFF3CD", "#856404"),
    "in_progress": ("#E3F2FD", "#0D47A1"),
    "success": ("#E8F5E9", "#1B5E20"),
    "failure": ("#FFEBEE", "#C62828"),
    "error": ("#FFEBEE", "#C62828"),
    "auto": ("#E0F7FA", "#006064"),
}

FAULT_PROFILE_LABELS = {
    "latency_spike": "Latency spike",
    "error_burst": "Application errors",
    "total_outage": "Total outage",
}

SETTINGS_DEFAULTS = {
    "settings_service": "demo-api",
    "settings_namespace": "default",
    "settings_region": "ap-south-1",
    "settings_host_port": "8080",
    "settings_container_port": "80",
    "settings_auto_low_risk": True,
    "settings_conf_thresh": 0.7,
    "settings_predictive_enabled": True,
}

for key, default_value in SETTINGS_DEFAULTS.items():
    st.session_state.setdefault(key, default_value)


# --- Session State bootstrap -------------------------------------------------
if "incidents" not in st.session_state:
    st.session_state.incidents: list[dict[str, Any]] = []
if "selected_incident_id" not in st.session_state:
    st.session_state.selected_incident_id: Optional[str] = None
if "activity_feed" not in st.session_state:
    st.session_state.activity_feed: list[dict[str, Any]] = []
if "health_history" not in st.session_state:
    st.session_state.health_history: list[dict[str, Any]] = []
if "auto_orchestrator" not in st.session_state:
    st.session_state.auto_orchestrator = {"incident_id": None, "armed": False}
else:
    st.session_state.auto_orchestrator.setdefault("incident_id", None)
    st.session_state.auto_orchestrator.setdefault("armed", False)
if "assistant_chat" not in st.session_state:
    st.session_state.assistant_chat: list[dict[str, Any]] = []
if "sidebar_tab" not in st.session_state:
    st.session_state.sidebar_tab = "Settings"


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


def rerun_app() -> None:
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


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


def inject_demo_fault(profile: str, duration_seconds: int) -> Optional[Dict[str, Any]]:
    payload = {"profile": profile, "duration_seconds": duration_seconds}
    ok, data, error = backend_request("POST", "/faults/inject", json=payload, timeout=10)
    if ok:
        return data or {}
    add_activity("Fault injection failed", "error", error)
    st.error(f"Failed to inject fault: {error}")
    return None


def reset_demo_fault() -> Optional[Dict[str, Any]]:
    ok, data, error = backend_request("POST", "/faults/reset", timeout=5)
    if ok:
        return data or {}
    add_activity("Fault reset failed", "error", error)
    st.error(f"Failed to reset fault: {error}")
    return None


def get_predictions() -> list[Dict[str, Any]]:
    ok, data, error = backend_request("GET", "/predict", timeout=5)
    if ok:
        return list(data or [])
    if error:
        add_activity("Prediction fetch failed", "error", error)
        st.error(f"Failed to fetch predictions: {error}")
    return []


def run_assistant_command(message: str) -> Optional[Dict[str, Any]]:
    payload = {"message": message}
    ok, data, error = backend_request("POST", "/assistant", json=payload, timeout=30)
    if ok:
        return data or {}
    add_activity("Assistant command failed", "error", error)
    st.error(f"Assistant error: {error}")
    return None


def assistant_chat_area() -> None:
    chat_container = st.container()
    history = st.session_state.assistant_chat[-20:]
    if not history:
        chat_container.info("Ask about health, incidents, or tell AutoMedic to restart services.")
    for entry in history:
        role = entry.get("role", "assistant")
        speaker = "You" if role == "user" else "HealOps"
        content = entry.get("content", "")
        chat_container.markdown(f"**{speaker}:** {content}")
        if role != "user":
            context = entry.get("context")
            if context:
                with chat_container.expander("Assistant context", expanded=False):
                    st.json(context)

    with st.form("assistant_chat_form", clear_on_submit=True):
        user_input = st.text_area(
            "Message",
            key="assistant_input",
            placeholder="e.g. HealOps, check if my EC2 app is healthy",
            height=80,
        )
        submitted = st.form_submit_button("Send", use_container_width=True)

    if submitted:
        message = (user_input or "").strip()
        if message:
            st.session_state.assistant_chat.append({"role": "user", "content": message})
            reply = run_assistant_command(message)
            if reply is not None:
                response_entry = {
                    "role": "assistant",
                    "content": reply.get("message", "(no response)"),
                    "context": reply.get("context"),
                    "intent": reply.get("intent"),
                    "confidence": reply.get("confidence"),
                }
                if reply.get("took_action"):
                    detail = reply.get("context", {}).get("message") or "Restart workflow dispatched"
                    add_activity("Assistant action", "success", detail)
                st.session_state.assistant_chat.append(response_entry)
        rerun_app()


def format_eta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    return f"{minutes}m"


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
    incident.setdefault("workflow_attempted", False)


def create_incident(
    current_health: Optional[dict[str, Any]] = None,
    reason_override: Optional[str] = None,
    *,
    auto: bool = False,
) -> Optional[dict[str, Any]]:
    snapshot = current_health or health
    if not snapshot:
        st.error("Cannot open an incident without health telemetry.")
        return None

    incident_id = f"INC-{len(st.session_state.incidents) + 1:03d}"
    thresholds = (snapshot or {}).get("thresholds", {})
    breach = "Manual trigger"
    error_rate_val = snapshot.get("error_rate") or 0.0
    p95_val = snapshot.get("p95_ms") or 0.0
    cpu_val = snapshot.get("cpu") or 0.0
    threshold_error = thresholds.get("error_rate_max", 0.05)
    threshold_p95 = thresholds.get("p95_ms_max", 1000)
    if error_rate_val > threshold_error:
        breach = f"error_rate {error_rate_val*100:.1f}% > {threshold_error*100:.1f}%"
    elif p95_val > threshold_p95:
        breach = f"p95 {p95_val}ms > {threshold_p95}ms"
    elif cpu_val and cpu_val > 85:
        breach = f"cpu {cpu_val}% > 85%"

    breach = reason_override or breach
    incident = {
        "id": incident_id,
        "opened_at": datetime.now(),
        "metric_breached": breach,
        "status": "open",
        "health_snapshot": snapshot,
        "service": service,
        "namespace": namespace,
        "region": region,
        "env": selected_env,
        "host_port": host_port,
        "container_port": container_port,
        "source": "auto" if auto else "manual",
    }
    st.session_state.incidents.append(incident)
    add_activity(f"Incident {incident_id} opened", "info", breach)
    return incident


def attempt_auto_dispatch(
    incident: dict[str, Any],
    action: Optional[str],
    confidence_value: float,
    incident_inputs: dict[str, Any],
    *,
    source: str,
) -> None:
    if incident.get("workflow_dispatched") or incident.get("workflow_attempted"):
        return

    action_name = "restart"
    if action_name not in ACTION_ENDPOINTS:
        return

    ok, data, error = trigger_backend_workflow(action_name, incident, incident_inputs)
    incident["workflow_attempted"] = True
    if ok:
        incident["workflow_dispatched"] = True
        incident["status"] = "resolving"
        incident["dispatched_action"] = action_name
        incident["workflow_status"] = {
            "state": "queued",
            "label": (data or {}).get("message", "Workflow queued"),
            "run_url": (data or {}).get("run_url") or GITHUB_ACTIONS_URL,
        }
        detail = f"{action_name} triggered via {source} (confidence {confidence_value:.2f})"
        add_activity(
            f"Workflow auto-dispatched for {incident['id']}",
            "success",
            detail,
        )
    else:
        add_activity(
            f"Auto-dispatch failed for {incident['id']}",
            "error",
            error,
        )


def orchestrate_auto_demo(
    health_snapshot: dict[str, Any],
    *,
    auto_low_risk: bool,
    conf_thresh: float,
    service: str,
    namespace: str,
    region: str,
    environment: str,
    host_port: str,
    container_port: str,
) -> None:
    if not health_snapshot:
        return

    state = st.session_state.auto_orchestrator
    incident_id = state.get("incident_id")
    armed = bool(state.get("armed"))

    fault_state = health_snapshot.get("fault_state") or {}
    fault_active = bool(fault_state.get("active"))

    if not armed and not incident_id:
        return

    if fault_active and armed:
        if not incident_id:
            incident = create_incident(
                current_health=health_snapshot,
                reason_override=(fault_state.get("status") or health_snapshot.get("status")),
                auto=True,
            )
            if not incident:
                return
            ensure_incident_defaults(incident)
            incident["auto_orchestrated"] = True
            incident["fault_profile"] = fault_state.get("profile")
            state["incident_id"] = incident["id"]
            st.session_state.selected_incident_id = incident["id"]
        incident = get_incident_by_id(state.get("incident_id"))
        if not incident:
            state["incident_id"] = None
            return
        ensure_incident_defaults(incident)
        incident["health_snapshot"] = health_snapshot
        state["armed"] = True

        if not incident.get("rca"):
            rca_result = run_rca(incident)
            if rca_result:
                incident["rca"] = rca_result
                incident["status"] = "diagnosed"
                add_activity(
                    f"RCA auto-complete for {incident['id']}",
                    "success",
                    f"Confidence {float(rca_result.get('confidence', 0.0)):.2f}",
                )

        rca_payload = incident.get("rca") or {}
        recommended_action = rca_payload.get("recommended_action")
        confidence_value = float(rca_payload.get("confidence", 0.0) or 0.0)

        if (
            incident.get("status") == "diagnosed"
            and not incident.get("workflow_dispatched")
            and auto_low_risk
            and not incident.get("workflow_attempted")
        ):
            incident_inputs = {
                "service": incident.get("service") or service,
                "namespace": incident.get("namespace") or namespace,
                "region": incident.get("region") or region,
                "environment": incident.get("env") or environment,
                "host_port": incident.get("host_port") or host_port,
                "container_port": incident.get("container_port") or container_port,
            }
            attempt_auto_dispatch(
                incident,
                recommended_action,
                confidence_value,
                incident_inputs,
                source="auto_orchestrator",
            )
        return

    if not incident_id:
        return

    incident = get_incident_by_id(incident_id)
    if not incident:
        state["incident_id"] = None
        state["armed"] = False
        return

    if health_snapshot.get("healthy") and incident.get("status") != "resolved":
        recovery_time = int((datetime.now() - incident["opened_at"]).total_seconds())
        incident["status"] = "resolved"
        incident["recovery_time"] = recovery_time
        add_activity(
            f"Incident {incident['id']} auto-resolved",
            "success",
            f"Recovered in {recovery_time}s",
        )

    if health_snapshot.get("healthy"):
        state["incident_id"] = None
        state["armed"] = False


# --- Layout: Header ----------------------------------------------------------
header_col1, header_col2, header_col3 = st.columns([2, 1, 1])

with header_col1:
    st.title("🩹 AutoMedic — self-healing demo")

with header_col2:
    selected_env = st.selectbox("Environment", ["prod", "stage"], key="env_selector")

with header_col3:
    st.markdown(f"[🔗 Last GitHub Action run]({GITHUB_ACTIONS_URL})")


st.divider()


# --- Sidebar -----------------------------------------------------------------
with st.sidebar:
    tab_choice = st.radio("Sidebar", ("Chat", "Settings"), index=0 if st.session_state.sidebar_tab == "Chat" else 1)
    st.session_state.sidebar_tab = tab_choice

    if tab_choice == "Chat":
        st.header("🗣️ Assistant")
        assistant_chat_area()
    else:
        st.header("Settings")
        st.text_input("Service", key="settings_service")
        st.text_input("Namespace", key="settings_namespace")
        st.text_input("Region", key="settings_region")
        st.text_input("Host port", key="settings_host_port")
        st.text_input("Container port", key="settings_container_port")

        st.toggle(
            "Auto-run low-risk fixes",
            value=st.session_state["settings_auto_low_risk"],
            key="settings_auto_low_risk",
        )
        st.slider(
            "Min confidence for auto-run",
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            value=st.session_state["settings_conf_thresh"],
            key="settings_conf_thresh",
        )
        st.toggle(
            "Enable Predictive Healing",
            value=st.session_state["settings_predictive_enabled"],
            key="settings_predictive_enabled",
        )

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
service = st.session_state["settings_service"]
namespace = st.session_state["settings_namespace"]
region = st.session_state["settings_region"]
host_port = st.session_state["settings_host_port"]
container_port = st.session_state["settings_container_port"]
auto_low_risk = bool(st.session_state["settings_auto_low_risk"])
conf_thresh = float(st.session_state["settings_conf_thresh"])
predictive_enabled = bool(st.session_state["settings_predictive_enabled"])

st.subheader("Live Health")
predictions: list[Dict[str, Any]] = []

fault_control_cols = st.columns([1.5, 1.0, 1.4])
with fault_control_cols[0]:
    fault_profile = st.selectbox(
        "Fault profile",
        list(FAULT_PROFILE_LABELS.keys()),
        format_func=lambda key: FAULT_PROFILE_LABELS.get(key, key),
        key="fault_profile_select",
    )
with fault_control_cols[1]:
    fault_duration = st.slider(
        "Duration (s)",
        min_value=10,
        max_value=180,
        value=45,
        step=5,
        key="fault_duration_slider",
    )
with fault_control_cols[2]:
    inject_clicked = st.button("🧨 Inject Fault", key="inject_fault_btn", use_container_width=True)
    reset_clicked = st.button("♻️ Reset Fault", key="reset_fault_btn", use_container_width=True)

if inject_clicked:
    state = inject_demo_fault(fault_profile, fault_duration)
    if state is not None:
        label = state.get("profile_label") or FAULT_PROFILE_LABELS.get(fault_profile, fault_profile)
        remaining = state.get("remaining_seconds", fault_duration)
        add_activity("Fault injected", "info", f"{label} for ~{remaining}s")
        st.success(f"Injected {label} fault for ~{remaining}s")
        orchestrator_state = st.session_state.auto_orchestrator
        orchestrator_state["armed"] = True
        orchestrator_state["incident_id"] = None
        orchestrator_state["fault_profile"] = state.get("profile")

if reset_clicked:
    state = reset_demo_fault()
    if state is not None:
        add_activity("Fault reset", "success")
        st.info("Cleared simulated fault")
        orchestrator_state = st.session_state.auto_orchestrator
        orchestrator_state["armed"] = False

health = get_health()

if health is None:
    st.warning("Unable to reach backend /health endpoint. Is the service running?")
else:
    record_health_history(health)
    orchestrate_auto_demo(
        health,
        auto_low_risk=auto_low_risk,
        conf_thresh=conf_thresh,
        service=service,
        namespace=namespace,
        region=region,
        environment=selected_env,
        host_port=host_port,
        container_port=container_port,
    )
    if predictive_enabled:
        predictions = get_predictions()

    col_status, col_metrics = st.columns([1.4, 2.6])

    with col_status:
        render_status_pill(bool(health.get("healthy")), health.get("status", "Unknown"))
        st.caption(
            f"Last probe: {int(health.get('last_probe_ms', 0))}ms | Error rate: {health.get('error_rate', 'n/a')}"
        )
        fault_state = health.get("fault_state") or {}
        if fault_state.get("active"):
            label = fault_state.get("profile_label") or fault_state.get("profile", "fault")
            remaining = fault_state.get("remaining_seconds")
            st.warning(f"Injected fault active: {label} ({remaining}s remaining)")

        if predictions:
            timers = [
                int(pred.get("time_to_cross_sec"))
                for pred in predictions
                if pred.get("crosses_threshold") and pred.get("time_to_cross_sec") is not None
            ]
            if timers:
                soonest = min(timers)
                st.warning(f"⚠️ Predicted degradation within ~{format_eta(soonest)}")

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

    if predictions:
        preds_df = pd.DataFrame(
            [
                {
                    "Metric": pred.get("metric"),
                    "ETA": format_eta(pred.get("time_to_cross_sec")),
                    "Projected": f"{float(pred.get('predicted_value', 0.0)):.3f}",
                    "Confidence": f"{float(pred.get('confidence', 0.0)):.2f}",
                }
                for pred in predictions
            ]
        )
        st.caption("Predictive forecasts (next 10 min)")
        st.dataframe(preds_df, hide_index=True, use_container_width=True)


st.divider()


# --- Incident Management -----------------------------------------------------
st.subheader("Incidents")

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
            ensure_incident_defaults(selected_incident)
            selected_incident["rca"] = rca_result
            selected_incident["status"] = "diagnosed"
            add_activity(
                f"RCA complete for {selected_incident['id']}",
                "success",
                f"Confidence {float(rca_result.get('confidence', 0.0)):.2f}",
            )

            confidence_value = float(rca_result.get("confidence", 0.0) or 0.0)
            recommended_action = rca_result.get("recommended_action")
            incident_inputs = {
                "service": selected_incident.get("service") or service,
                "namespace": selected_incident.get("namespace") or namespace,
                "region": selected_incident.get("region") or region,
                "environment": selected_incident.get("env") or selected_env,
                "host_port": selected_incident.get("host_port") or host_port,
                "container_port": selected_incident.get("container_port") or container_port,
            }
            if auto_low_risk:
                attempt_auto_dispatch(
                    selected_incident,
                    recommended_action,
                    confidence_value,
                    incident_inputs,
                    source="rca_high_confidence",
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

            workflow_attempted = bool(selected_incident.get("workflow_attempted"))

            if workflow_state:
                render_status_chip(
                    workflow_state.get("label", workflow_state.get("state", "queued")),
                    workflow_state.get("state", "queued"),
                )
                run_link = workflow_state.get("run_url")
                if run_link:
                    st.markdown(f"[View run ↗]({run_link})")
                st.caption("Auto-remediation in progress")
            elif workflow_attempted:
                st.caption("Auto-remediation requested; awaiting workflow status…")
            elif (
                recommended_action in LOW_RISK_ACTIONS
                and recommended_action in ACTION_ENDPOINTS
                and confidence_value >= conf_thresh
                and auto_low_risk
            ):
                st.caption("Low-risk fix meets threshold. Manual approval available if needed.")

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
                disabled=selected_incident.get("workflow_dispatched") or selected_incident.get("workflow_attempted"),
            )

            if st.button(
                "Approve & Execute",
                key=f"dispatch_{selected_incident['id']}",
                disabled=selected_incident.get("workflow_dispatched") or selected_incident.get("workflow_attempted"),
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
