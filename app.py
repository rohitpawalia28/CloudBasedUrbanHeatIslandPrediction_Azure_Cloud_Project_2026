"""
Vercel serverless entry point.

Vercel auto-detects this Flask entrypoint (root app.py exposing `app`) and routes every request to it.
On the first request of a new instance the emulated AWS account is created in /tmp and the full Step Functions
pipeline (ingest -> ETL -> train -> deploy -> batch predict) runs once (~20-40 s), after
which the warm instance serves the console and REST API instantly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.backend import cloud_setup  # noqa: E402
from src.backend.app import create_app  # noqa: E402

import threading  # noqa: E402

app = create_app()
_boot_lock = threading.Lock()
_booted = False


@app.before_request
def _bootstrap_cloud_account():
    """Build the emulated AWS account on the first request (not at import time:
    serverless platforms cap the init phase at ~10 s, the pipeline needs ~20-40 s)."""
    global _booted
    if _booted:
        return
    with _boot_lock:
        if _booted:
            return
        cloud_setup.provision()
        if cloud_setup.needs_first_run():
            cloud_setup.run_pipeline("vercel cold start", wait=True)
        _booted = True


if __name__ == "__main__":   # local use: prefer  python run.py
    app.run(debug=False)
