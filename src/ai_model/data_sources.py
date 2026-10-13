"""
Raw data acquisition for the Delhi NCR study area.

Produces three raw source files that land in the S3 *raw* zone, in the same shape
the real sources deliver them:

  raw/landsat/LC09_L2SP_<date>_indices.csv   Landsat 8/9 Collection-2 Level-2 derived
                                             grid indices (NDVI, NDBI, NDWI, albedo),
                                             surface temperature in KELVIN and a QA
                                             cloud flag (cloudy cells must be masked)
  raw/meteorology/station_daily.csv          Daily weather at 5 IMD-area stations
                                             (Tmax, RH, wind) - replace with real data
                                             via scripts/fetch_real_weather.py
  raw/static/lulc_dem_hydro.csv              Land-use class, elevation, distance to
                                             the Yamuna and urban-density proxy

The bundled sample is a physically-calibrated reference sample: urban cores,
the Yamuna river, the Delhi Ridge and Aravalli forests, industrial clusters and the
airport are placed at their real coordinates, and LST is generated from published
UHI relationships (vegetation cooling, built-up warming, water-body cooling decay,
wind dampening and hot-day amplification). It lets the whole cloud pipeline run
offline; swap in real exports with the same columns at any time.
"""
import io
import math

import numpy as np
import pandas as pd

from src import config

SEED = 2026
OBS_DATES = ["2023-04-15", "2023-05-17", "2023-06-02", "2024-04-17", "2024-05-19", "2024-06-04"]

URBAN_CORES = [  # (lat, lon, strength, spread_km, name)
    (28.6315, 77.2167, 1.00, 5.5, "Connaught Place"),
    (28.6562, 77.2300, 1.00, 3.5, "Old Delhi"),
    (28.4950, 77.0890, 0.90, 4.5, "Gurugram Cyber City"),
    (28.5700, 77.3210, 0.85, 4.5, "Noida"),
    (28.5921, 77.0460, 0.70, 3.5, "Dwarka"),
    (28.7360, 77.1130, 0.78, 4.0, "Rohini"),
    (28.6500, 77.3000, 0.80, 3.5, "Shahdara / East Delhi"),
    (28.5450, 77.2000, 0.65, 3.5, "South Delhi"),
]
INDUSTRIAL = [(28.5300, 77.2750, "Okhla Industrial Area"), (28.8500, 77.0950, "Narela Industrial Area"),
              (28.6800, 77.1500, "Wazirpur / Naraina"), (28.4580, 77.0300, "Udyog Vihar")]
AIRPORT = (28.5562, 77.1000)
YAMUNA = [(28.90, 77.205), (28.80, 77.215), (28.72, 77.235), (28.67, 77.245), (28.62, 77.255),
          (28.58, 77.270), (28.54, 77.300), (28.49, 77.320), (28.44, 77.330), (28.38, 77.335)]
FORESTS = [  # (lat_min, lat_max, lon_min, lon_max, name)
    (28.615, 28.680, 77.165, 77.205, "Central Ridge"),
    (28.450, 28.525, 77.180, 77.265, "Asola Bhatti / Southern Ridge"),
    (28.400, 28.470, 77.060, 77.160, "Aravalli Biodiversity Park"),
    (28.535, 28.560, 77.140, 77.170, "Sanjay Van"),
]
STATIONS = [("Safdarjung", 28.5800, 77.2000), ("Palam", 28.5600, 77.1100), ("Gurugram", 28.4600, 77.0300),
            ("Noida", 28.5700, 77.3200), ("Narela", 28.8500, 77.0900)]
# Regional daily max temperature, humidity, wind per acquisition date (pre-monsoon Delhi climatology)
WEATHER_BASE = {
    "2023-04-15": (39.2, 22, 3.4), "2023-05-17": (44.1, 18, 3.9), "2023-06-02": (40.6, 38, 4.6),
    "2024-04-17": (38.4, 26, 3.1), "2024-05-19": (45.7, 16, 2.8), "2024-06-04": (43.9, 30, 3.6),
}


def _km(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111.0
    dlon = (lon2 - lon1) * 111.0 * math.cos(math.radians(28.6))
    return np.sqrt(dlat ** 2 + dlon ** 2)


def _dist_to_polyline_m(lat, lon, poly):
    best = np.full(lat.shape, np.inf)
    for (a_lat, a_lon), (b_lat, b_lon) in zip(poly[:-1], poly[1:]):
        for t in np.linspace(0, 1, 40):
            p_lat, p_lon = a_lat + t * (b_lat - a_lat), a_lon + t * (b_lon - a_lon)
            best = np.minimum(best, _km(lat, lon, p_lat, p_lon))
    return best * 1000


def build_static(rng):
    a = config.STUDY_AREA
    n = a["grid_size"]
    lats = np.linspace(a["lat_max"], a["lat_min"], n)
    lons = np.linspace(a["lon_min"], a["lon_max"], n)
    LON, LAT = np.meshgrid(lons, lats)
    lat, lon = LAT.ravel(), LON.ravel()
    rows, cols = np.divmod(np.arange(n * n), n)

    density = np.zeros_like(lat)
    for c_lat, c_lon, s, spread, _ in URBAN_CORES:
        density += s * np.exp(-(_km(lat, lon, c_lat, c_lon) / spread) ** 2)
    density = np.clip(density + rng.normal(0, 0.05, lat.shape), 0, 1)

    dist_water = _dist_to_polyline_m(lat, lon, YAMUNA)
    forest = np.zeros(lat.shape, bool)
    for la0, la1, lo0, lo1, _ in FORESTS:
        forest |= (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
    forest &= rng.random(lat.shape) > 0.12
    industrial = np.zeros(lat.shape, bool)
    for i_lat, i_lon, _ in INDUSTRIAL:
        industrial |= _km(lat, lon, i_lat, i_lon) < 2.2
    airport = _km(lat, lon, *AIRPORT) < 2.0

    lulc = np.full(lat.shape, 3)                                   # open land
    lulc[density > 0.30] = 4                                       # low-density urban
    lulc[density > 0.60] = 5                                       # high-density urban
    agri = (density < 0.22) & (rng.random(lat.shape) > 0.25)
    lulc[agri] = 2
    lulc[industrial & (density > 0.15)] = 6
    lulc[airport] = 3
    lulc[forest] = 1
    lulc[dist_water < 450] = 0

    elevation = 212 + 18 * np.clip((28.62 - lat) / 0.22, 0, 1) ** 1.3 * 4.5 \
        + np.where(lulc == 1, 25, 0) - 6 * np.exp(-dist_water / 1500) + rng.normal(0, 2.5, lat.shape)
    density[lulc == 0] = 0.02

    return pd.DataFrame({
        "grid_id": [f"DNCR-{r:02d}-{c:02d}" for r, c in zip(rows, cols)],
        "latitude": lat.round(5), "longitude": lon.round(5),
        "lulc_code": lulc, "lulc_class": [config.LULC_CLASSES[int(x)] for x in lulc],
        "elevation_m": elevation.round(1), "distance_to_water_m": dist_water.round(0),
        "urban_density": density.round(3),
    })


def build_landsat(static, date, rng):
    season = {"04": 1.0, "05": 0.85, "06": 0.92}[date[5:7]]    # pre-monsoon vegetation stress
    l = static["lulc_code"].to_numpy()
    n = len(l)
    ndvi_mu = np.select([l == 0, l == 1, l == 2, l == 3, l == 4, l == 5, l == 6],
                        [-0.08, 0.60, 0.42 * season, 0.15, 0.25, 0.12, 0.08])
    ndvi = np.clip(ndvi_mu - 0.10 * static["urban_density"].to_numpy() + rng.normal(0, 0.05, n), -0.3, 0.85)
    ndbi = np.clip(np.select([l == 0, l == 1, l == 2, l == 3], [-0.40, -0.25, -0.10, 0.05], 0.18)
                   + 0.18 * static["urban_density"].to_numpy() - 0.3 * np.maximum(ndvi, 0) + rng.normal(0, 0.04, n), -0.6, 0.6)
    ndwi = np.clip(np.where(l == 0, 0.38, -0.85 * ndvi - 0.05) + rng.normal(0, 0.04, n), -0.8, 0.7)
    albedo = np.clip(np.select([l == 0, l == 1, l == 2, l == 3, l == 6], [0.06, 0.12, 0.17, 0.26, 0.20], 0.16)
                     + rng.normal(0, 0.015, n), 0.04, 0.4)

    tair_base, rh_base, wind_base = WEATHER_BASE[date]
    dens = static["urban_density"].to_numpy()
    tair = tair_base + 0.9 * dens + rng.normal(0, 0.3, n)
    wind = np.clip(wind_base * (1 - 0.35 * dens) + rng.normal(0, 0.3, n), 0.3, None)
    rh = np.clip(rh_base - 4 * dens + 6 * np.exp(-static["distance_to_water_m"].to_numpy() / 1200)
                 + rng.normal(0, 1.5, n), 5, 95)

    lst = (tair + 1.0
           + 6.0 * dens ** 1.6 * (1 - 0.45 * np.tanh(wind / 3))
           - 4.5 * np.tanh(3.0 * np.maximum(ndvi, 0))
           + 3.5 * np.maximum(ndbi, 0) * (1 + 2.0 * np.maximum(0.2 - albedo, 0) * 5)
           - 10.0 * (albedo - 0.15)
           - 3.0 * np.exp(-static["distance_to_water_m"].to_numpy() / 700)
           - 0.02 * (static["elevation_m"].to_numpy() - 215)
           + np.where(l == 6, 2.0, 0) + np.where(l == 3, 1.5, 0)
           + 0.30 * (tair_base - 40) * dens ** 2
           - 0.03 * np.maximum(rh - 25, 0)
           + rng.normal(0, 0.55, n))
    lst = np.where(l == 0, tair - 7 + rng.normal(0, 0.5, n), lst)

    cloud = rng.random(n) < (0.06 if date[5:7] == "06" else 0.02)
    df = pd.DataFrame({
        "grid_id": static["grid_id"], "acquisition_date": date, "scene_id": f"LC09_L2SP_146040_{date.replace('-', '')}_02_T1",
        "ndvi": ndvi.round(4), "ndbi": ndbi.round(4), "ndwi": ndwi.round(4), "albedo": albedo.round(4),
        "st_kelvin": (lst + 273.15).round(2), "qa_cloud": cloud.astype(int),
    })
    # Simulate sensor gaps / scan-line drop-outs
    for col in ["ndvi", "ndbi", "albedo"]:
        df.loc[rng.random(n) < 0.01, col] = np.nan
    return df, {"tair": tair, "rh": rh, "wind": wind}


def build_station_weather(rng):
    real = config.BASE_DIR / "data" / "real" / "station_daily.csv"
    if real.exists():  # produced by scripts/fetch_real_weather.py
        return pd.read_csv(real)
    rows = []
    for date, (t, rh, w) in WEATHER_BASE.items():
        for name, s_lat, s_lon in STATIONS:
            urban = 0.6 if name in ("Safdarjung", "Noida") else 0.35
            rows.append({"date": date, "station": name, "latitude": s_lat, "longitude": s_lon,
                         "temperature_2m_max": round(t + 0.9 * urban + rng.normal(0, 0.4), 1),
                         "relative_humidity_2m_mean": round(rh - 4 * urban + rng.normal(0, 1.2), 1),
                         "wind_speed_10m_mean_ms": round(max(0.5, w * (1 - 0.35 * urban) + rng.normal(0, 0.3)), 2),
                         "source": "reference-sample (replace with Open-Meteo / IMD)"})
    return pd.DataFrame(rows)


def generate_raw_files():
    """Return {s3_key: csv_bytes} for every raw source file."""
    rng = np.random.default_rng(SEED)
    static = build_static(rng)
    files = {"raw/static/lulc_dem_hydro.csv": static}
    for date in OBS_DATES:
        landsat, _ = build_landsat(static, date, rng)
        files[f"raw/landsat/LC09_L2SP_{date.replace('-', '')}_indices.csv"] = landsat
    files["raw/meteorology/station_daily.csv"] = build_station_weather(rng)
    out = {}
    for key, df in files.items():
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        out[key] = buf.getvalue().encode()
    return out
