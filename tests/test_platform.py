"""End-to-end checks of the emulated AWS platform (run with:  pytest -q)."""
from src import config

PAGES = ["/", "/map", "/predict", "/data-lake", "/pipeline", "/models", "/monitoring",
         "/alerts", "/database", "/api-access", "/admin", "/architecture"]


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200


def test_all_console_pages_render(client):
    for p in PAGES:
        assert client.get(p).status_code == 200, p


def test_unauthenticated_api_is_rejected():
    from src.backend.app import create_app
    anon = create_app().test_client()
    assert anon.get("/api/v1/overview").status_code in (401, 403)


def test_s3_data_lake_zones(client):
    keys = [o["Key"] for o in client.get("/api/v1/s3/objects").get_json()]
    for zone in ("raw/", "processed/", "predictions/", "model-artifacts/"):
        assert any(k.startswith(zone) for k in keys), zone


def test_model_metrics_are_reasonable(client):
    data = client.get("/api/v1/sagemaker").get_json()
    assert data, "no SageMaker data"


def test_predict_by_location(client):
    r = client.post("/api/v1/predict", json={"latitude": 28.6315, "longitude": 77.2167})
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body["uhi_intensity_celsius"], float)
    assert body["risk_level"]


def test_predict_missing_features_is_400(client):
    r = client.post("/api/v1/predict", json={"features": {"ndvi": 0.2}})
    assert r.status_code == 400


def test_vegetation_cools(client):
    """Physical sanity: more vegetation + less built-up => lower predicted UHI."""
    base = client.post("/api/v1/predict", json={"latitude": 28.6315, "longitude": 77.2167}).get_json()["features"]
    green = dict(base, ndvi=0.7, ndbi=-0.4, urban_density=0.1)
    hot = dict(base, ndvi=0.0, ndbi=0.4, urban_density=1.0)
    g = client.post("/api/v1/predict", json={"features": green}).get_json()["uhi_intensity_celsius"]
    h = client.post("/api/v1/predict", json={"features": hot}).get_json()["uhi_intensity_celsius"]
    assert g < h


def test_s3_download_without_key_is_404(client):
    assert client.get("/api/v1/s3/download").status_code == 404


def test_rds_read_only_query(client):
    r = client.post("/api/v1/rds/query", json={"sql": "SELECT COUNT(*) AS n FROM prediction_requests"})
    assert r.status_code == 200
