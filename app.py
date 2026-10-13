"""
Vercel serverless entry point.

Vercel auto-detects this Flask entrypoint (root app.py exposing `app`) and routes every request to it.
On a cold start the emulated AWS account is created in /tmp and the full Step Functions
pipeline (ingest -> ETL -> train -> deploy -> batch predict) runs once (~20-40 s), after
which the warm instance serves the console and REST API instantly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.backend import cloud_setup  # noqa: E402
from src.backend.app import create_app  # noqa: E402

cloud_setup.provision()
if cloud_setup.needs_first_run():
    cloud_setup.run_pipeline("vercel cold start", wait=True)

app = create_app()


if __name__ == "__main__":   # local use: prefer  python run.py
    app.run(debug=False)
