"""
Vercel serverless entry point.

Vercel runs this file as a Python function; every request is routed here by vercel.json.
On a cold start the emulated AWS account is created in /tmp and the full Step Functions
pipeline (ingest -> ETL -> train -> deploy -> batch predict) runs once (~20-40 s), after
which the warm instance serves the console and REST API instantly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backend import cloud_setup  # noqa: E402
from src.backend.app import create_app  # noqa: E402

cloud_setup.provision()
if cloud_setup.needs_first_run():
    cloud_setup.run_pipeline("vercel cold start", wait=True)

app = create_app()
