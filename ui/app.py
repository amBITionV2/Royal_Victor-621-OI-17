"""Streamlit dashboard for HealOps incident monitoring and remediation."""

import streamlit as st

st.set_page_config(page_title="HealOps Dashboard", layout="wide")

st.title("HealOps")
st.caption("AI-powered self-healing infrastructure control plane")

with st.sidebar:
    st.header("Incident Filters")
    st.selectbox("Severity", ["all", "low", "medium", "high", "critical"], index=0)
    st.checkbox("Only active incidents", value=True)

st.subheader("Recent Incidents")
st.info("Connect to the backend API to list incidents detected by the watcher.")

col1, col2 = st.columns(2)

with col1:
    st.subheader("Root Cause Analysis")
    st.write("Select an incident to view the Gemini-generated RCA summary.")

with col2:
    st.subheader("Automated Actions")
    st.write("Trigger GitHub Actions workflows for remediation from here.")
