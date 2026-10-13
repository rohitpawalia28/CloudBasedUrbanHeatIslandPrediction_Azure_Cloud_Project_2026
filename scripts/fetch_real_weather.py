"""
Download REAL daily weather for the 5 Delhi NCR stations from the Open-Meteo
historical archive (ERA5 reanalysis, free, no API key) for every Landsat
acquisition date used by the project.

    python scripts/fetch_real_weather.py            # download -> data/real/station_daily.csv
    python run.py --reset                           # rebuild the cloud account with real weather

After the CSV exists, the ingestion Lambda uses it instead of the bundled
reference sample (the rest of the pipeline is unchanged).
"""
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ai_model.data_sources import OBS_DATES, STATIONS  # noqa: E402

URL = "https://archive-api.open-meteo.com/v1/archive"
OUT = ROOT / "data" / "real" / "station_daily.csv"


def main():
    rows = []
    for name, lat, lon in STATIONS:
        for date in OBS_DATES:
            r = requests.get(URL, timeout=30, params={
                "latitude": lat, "longitude": lon, "start_date": date, "end_date": date,
                "daily": "temperature_2m_max,relative_humidity_2m_mean,wind_speed_10m_mean",
                "wind_speed_unit": "ms", "timezone": "Asia/Kolkata"})
            r.raise_for_status()
            d = r.json()["daily"]
            rows.append({"date": date, "station": name, "latitude": lat, "longitude": lon,
                         "temperature_2m_max": d["temperature_2m_max"][0],
                         "relative_humidity_2m_mean": d["relative_humidity_2m_mean"][0],
                         "wind_speed_10m_mean_ms": d["wind_speed_10m_mean"][0],
                         "source": "Open-Meteo ERA5 archive"})
            print(f"  {date}  {name:<11} Tmax={rows[-1]['temperature_2m_max']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT, index=False)
    print(f"\nSaved {len(rows)} real observations -> {OUT.relative_to(ROOT)}")
    print("Now run:  python run.py --reset")


if __name__ == "__main__":
    main()
