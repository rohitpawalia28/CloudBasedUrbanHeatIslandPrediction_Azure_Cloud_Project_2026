"""
Glue ETL job: raw zone -> processed (model-ready) zone.

Steps (mirrors dataset/README.md "Planned preprocessing"):
  1. Read every Landsat scene in raw/landsat/, convert ST from Kelvin to Celsius
  2. Apply the QA cloud mask (drop cloudy cells) and flag missing observations
  3. Impute missing spectral indices with the per-land-use-class median
  4. Spatially interpolate station weather to every grid cell (inverse distance weighting)
  5. Join static land-use / DEM / hydrology layers on grid_id
  6. Compute the target: UHI intensity = LST(cell) - mean rural LST (forest + agriculture) per date
  7. Remove physically impossible values, write processed/uhi_model_ready.csv,
     a data-quality report and a dataset manifest
"""
import io
import json

import numpy as np
import pandas as pd

from src import config
from src.aws_cloud import s3

B = config.S3_BUCKET


def _read_csv(key):
    return pd.read_csv(s3.local_path(B, key))


def _idw(cell_lat, cell_lon, st_lat, st_lon, st_val, power=2):
    d = np.sqrt((cell_lat[:, None] - st_lat[None, :]) ** 2 + (cell_lon[:, None] - st_lon[None, :]) ** 2) + 1e-6
    w = 1 / d ** power
    return (w * st_val[None, :]).sum(1) / w.sum(1)


def run_etl(args, log):
    static = _read_csv("raw/static/lulc_dem_hydro.csv")
    weather = _read_csv("raw/meteorology/station_daily.csv")
    scenes = [o["Key"] for o in s3.list_objects(B, "raw/landsat/") if o["Key"].endswith(".csv")]
    if not scenes:
        raise RuntimeError("No Landsat scenes found in raw/landsat/")
    log(f"Found {len(scenes)} Landsat scene(s), {len(static)} grid cells, {len(weather)} weather rows")

    quality = {"scenes": len(scenes), "cells": int(len(static)), "per_scene": []}
    frames = []
    for key in sorted(scenes):
        df = _read_csv(key)
        need = {"grid_id", "acquisition_date", "ndvi", "ndbi", "ndwi", "albedo", "st_kelvin", "qa_cloud"}
        if not need.issubset(df.columns) or df.empty:
            log(f"Skipping {key}: missing columns {sorted(need - set(df.columns))}", "WARN")
            continue
        date = str(df["acquisition_date"].iloc[0])
        n0 = len(df)
        cloudy = int(df["qa_cloud"].sum())
        df = df[df["qa_cloud"] == 0].copy()
        missing = int(df[["ndvi", "ndbi", "albedo"]].isna().any(axis=1).sum())
        df["lst_celsius"] = (df["st_kelvin"] - 273.15).round(2)

        df = df.merge(static, on="grid_id", how="inner")
        for col in ["ndvi", "ndbi", "ndwi", "albedo"]:
            df[col] = df[col].fillna(df.groupby("lulc_code")[col].transform("median"))

        w = weather[weather["date"] == date]
        if w.empty:
            raise RuntimeError(f"No meteorology rows for {date} - upload station data for this date")
        la, lo = df["latitude"].to_numpy(), df["longitude"].to_numpy()
        sla, slo = w["latitude"].to_numpy(), w["longitude"].to_numpy()
        df["air_temperature_celsius"] = _idw(la, lo, sla, slo, w["temperature_2m_max"].to_numpy()).round(2)
        df["humidity_percent"] = _idw(la, lo, sla, slo, w["relative_humidity_2m_mean"].to_numpy()).round(1)
        df["wind_speed_ms"] = _idw(la, lo, sla, slo, w["wind_speed_10m_mean_ms"].to_numpy()).round(2)

        rural = df[df["lulc_code"].isin([1, 2])]["lst_celsius"].mean()
        df["rural_reference_lst"] = round(float(rural), 2)
        df["uhi_intensity_celsius"] = (df["lst_celsius"] - rural).round(2)
        df["timestamp"] = date
        frames.append(df)
        quality["per_scene"].append({"scene": key.split("/")[-1], "date": date, "rows_in": n0,
                                     "cloud_masked": cloudy, "imputed_rows": missing,
                                     "rural_reference_lst": round(float(rural), 2)})
        log(f"{date}: {n0} cells, {cloudy} cloud-masked, {missing} imputed, rural LST ref {rural:.2f} C")

    if not frames:
        raise RuntimeError("No valid Landsat scenes to process")
    data = pd.concat(frames, ignore_index=True)
    before = len(data)
    data = data[(data["lst_celsius"].between(10, 75)) & (data["ndvi"].between(-1, 1))]
    quality["outliers_removed"] = int(before - len(data))

    cols = ["grid_id", "timestamp", "latitude", "longitude", "lst_celsius", "uhi_intensity_celsius",
            "ndvi", "ndbi", "ndwi", "lulc_code", "lulc_class", "elevation_m", "albedo",
            "distance_to_water_m", "urban_density", "air_temperature_celsius", "humidity_percent",
            "wind_speed_ms", "rural_reference_lst"]
    data = data[cols]
    buf = io.StringIO()
    data.to_csv(buf, index=False)
    s3.put_object(B, "processed/uhi_model_ready.csv", buf.getvalue(), "text/csv",
                  metadata={"producer": "glue:uhi-etl-preprocess"}, fire_events=False)

    quality.update({"rows_out": int(len(data)), "features": len(config.FEATURES),
                    "uhi_mean": round(float(data["uhi_intensity_celsius"].mean()), 3),
                    "uhi_min": round(float(data["uhi_intensity_celsius"].min()), 3),
                    "uhi_max": round(float(data["uhi_intensity_celsius"].max()), 3),
                    "missing_after_etl": int(data.isna().sum().sum())})
    s3.put_object(B, "processed/data_quality_report.json", json.dumps(quality, indent=2), "application/json", fire_events=False)
    manifest = {"dataset": "uhi_model_ready", "study_area": config.STUDY_AREA["name"], "crs": config.STUDY_AREA["crs"],
                "rows": int(len(data)), "columns": len(cols), "dates": sorted(data["timestamp"].unique().tolist()),
                "target": config.TARGET, "features": config.FEATURES,
                "sources": ["raw/landsat/*", "raw/meteorology/station_daily.csv", "raw/static/lulc_dem_hydro.csv"]}
    s3.put_object(B, "processed/dataset_manifest.json", json.dumps(manifest, indent=2), "application/json", fire_events=False)
    log(f"Wrote processed/uhi_model_ready.csv: {len(data)} rows x {len(cols)} columns; "
        f"UHI range {quality['uhi_min']} .. {quality['uhi_max']} C")
    return {"rows": int(len(data)), "columns": len(cols), "output": f"s3://{B}/processed/uhi_model_ready.csv",
            "quality": quality}
