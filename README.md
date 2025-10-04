# HealOps

AI-powered self-healing infrastructure system designed for hackathon experimentation. HealOps monitors services, performs root-cause analysis (RCA) with Gemini, and triggers GitHub Actions workflows for automated remediation.

## Features
- FastAPI backend for incident intake, RCA orchestration, and workflow dispatch
- Gemini-powered root-cause analysis utilities
- Streamlit dashboard to review incidents and trigger actions
- GitHub Actions workflows for restart, rollback, and scale-up automation

## Getting Started
1. **Clone & Install**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configure Environment**
   Copy `.env.example` to `.env` and fill in Gemini and GitHub credentials.

## Running the Project
- **Backend API**
  ```bash
  uvicorn healops.backend.main:app --reload
  ```
- **Streamlit UI**
  ```bash
  streamlit run healops/ui/app.py
  ```

## Tests
Run unit tests with pytest (install separately if needed):
```bash
pytest
```

## Data & Observability
- `data/incidents.db` placeholder for an incident SQLite database
- `data/sample_logs.log` example service logs for prototyping

## Hackathon Notes
Use the provided GitHub Actions workflows as starting points for automation. Customize RCA parsing and monitoring logic to integrate with your infrastructure.
