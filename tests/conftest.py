import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from src.backend import cloud_setup
    from src.backend.app import create_app
    cloud_setup.provision()
    if cloud_setup.needs_first_run():           # first run trains the models (~30-60 s)
        cloud_setup.run_pipeline("pytest", wait=True)
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"email": config.ADMIN_EMAIL, "password": config.ADMIN_PASSWORD})
    return c
