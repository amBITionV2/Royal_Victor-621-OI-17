#!/usr/bin/env python3
"""Entrypoint script to run the HealOps FastAPI server."""

import os
import logging
from dotenv import load_dotenv
import uvicorn

# Suppress Google gRPC warnings
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GRPC_TRACE"] = ""

# Load environment variables from .env file
load_dotenv()

from backend.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True,  # Enable auto-reload for development
        log_level="info"
    )
