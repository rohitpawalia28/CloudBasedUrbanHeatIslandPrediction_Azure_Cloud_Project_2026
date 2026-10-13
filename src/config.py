"""
Central configuration for the Cloud-Based UHI Prediction platform.

The platform runs in two modes:
  * CLOUD_MODE=emulated (default) - every AWS service (S3, Lambda, Glue, SageMaker,
    RDS, Cognito, API Gateway, CloudWatch, SNS, Step Functions, IAM) is provided by
    the local emulation layer in ``src/aws_cloud``. No AWS account is needed.
  * CLOUD_MODE=aws - S3 object storage and SNS publishing are mirrored to a real AWS
    account through boto3 (see ``src/aws_cloud/aws_adapter.py``). Credentials are read
    from the standard AWS credential chain, never from this repository.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# ---- General -----------------------------------------------------------------
PROJECT_NAME = "Cloud-Based Urban Heat Island Prediction"
PROJECT_CODE = "uhi-geoai"
COURSE = "BITE412L - Cloud Computing"
AUTHOR = "Rohit Kumar (23BIT0030)"

CLOUD_MODE = os.getenv("CLOUD_MODE", "emulated").lower()
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "123456789012")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-uhi-geoai-local-dev-secret")

# ---- Local cloud runtime -----------------------------------------------------
# On Vercel (serverless) the code directory is read-only, so the emulated account lives in /tmp.
SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
CLOUD_ROOT = Path(os.getenv("CLOUD_ROOT") or ("/tmp/uhi_cloud_runtime" if SERVERLESS else BASE_DIR / "cloud_runtime"))
S3_ROOT = CLOUD_ROOT / "s3"
RDS_PATH = CLOUD_ROOT / "rds" / "uhi_metadata.db"
CLOUDWATCH_ROOT = CLOUD_ROOT / "cloudwatch"

S3_BUCKET = os.getenv("S3_BUCKET", "uhi-geoai-data-lake")
S3_PREFIXES = ["raw/", "processed/", "model-artifacts/", "predictions/", "logs/"]

# ---- Study area: Delhi NCR (Delhi, Gurugram, Noida) --------------------------
STUDY_AREA = {
    "name": "Delhi NCR, India",
    "lat_min": 28.40, "lat_max": 28.88,
    "lon_min": 76.94, "lon_max": 77.38,
    "grid_size": 48,          # 48 x 48 cells (~1 km cells)
    "crs": "EPSG:4326",
}

# ---- Model -------------------------------------------------------------------
FEATURES = [
    "ndvi", "ndbi", "ndwi", "albedo", "elevation_m", "distance_to_water_m",
    "urban_density", "air_temperature_celsius", "humidity_percent",
    "wind_speed_ms", "lulc_code",
]
TARGET = "uhi_intensity_celsius"

LULC_CLASSES = {
    0: "Water", 1: "Forest / Ridge", 2: "Agriculture", 3: "Open land",
    4: "Low-density urban", 5: "High-density urban", 6: "Industrial",
}

# ---- Heat risk thresholds (decision support only) ----------------------------
HEAT_RISK_LEVELS = [
    (1.0, "Low"), (3.0, "Moderate"), (5.0, "High"), (99.0, "Severe"),
]
DEFAULT_ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "5.0"))

# ---- API Gateway -------------------------------------------------------------
API_RATE_LIMIT_PER_MIN = int(os.getenv("API_RATE_LIMIT_PER_MIN", "120"))
TOKEN_TTL_SECONDS = 60 * 60 * 8

# ---- Default admin (Cognito user pool seed) ----------------------------------
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@uhi.cloud")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Admin@123")


def risk_level(uhi_value: float) -> str:
    for limit, label in HEAT_RISK_LEVELS:
        if uhi_value < limit:
            return label
    return "Severe"
