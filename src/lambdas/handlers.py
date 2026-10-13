"""
AWS Lambda function handlers for the UHI platform.

uhi-ingest-raw-data   Lands the source datasets in the S3 raw zone
uhi-validate-upload   S3 ObjectCreated trigger on raw/*.csv - schema & quality checks,
                      then starts the Glue ETL job for valid uploads
uhi-heat-alert        Evaluates the newest predictions against the heat threshold and
                      publishes an SNS alert listing the hottest grid cells
"""
import io
import json

import pandas as pd

from src import config
from src.aws_cloud import cloudwatch, glue, rds, s3, sns

B = config.S3_BUCKET

REQUIRED_COLUMNS = {
    "raw/landsat/": ["grid_id", "acquisition_date", "ndvi", "ndbi", "ndwi", "albedo", "st_kelvin", "qa_cloud"],
    "raw/meteorology/": ["date", "station", "latitude", "longitude", "temperature_2m_max",
                         "relative_humidity_2m_mean", "wind_speed_10m_mean_ms"],
    "raw/static/": ["grid_id", "latitude", "longitude", "lulc_code", "elevation_m", "distance_to_water_m", "urban_density"],
}

SETTINGS = {"auto_etl_on_upload": True, "alert_threshold": config.DEFAULT_ALERT_THRESHOLD}


def ingest_raw_data(event, context):
    from src.ai_model.data_sources import generate_raw_files
    force = bool(event.get("force"))
    existing = {o["Key"] for o in s3.list_objects(B, "raw/")}
    written, kept = [], []
    for key, body in generate_raw_files().items():
        if key in existing and not force:
            kept.append(key)
            continue
        s3.put_object(B, key, body, "text/csv", metadata={"source": "data-acquisition"}, fire_events=False)
        written.append(key)
    context.log(f"Ingest complete: {len(written)} written, {len(kept)} already present")
    return {"written": written, "kept": kept, "raw_objects": len(s3.list_objects(B, "raw/"))}


def validate_upload(event, context):
    rec = event["Records"][0]
    key = rec["s3"]["object"]["key"]
    folder = next((p for p in REQUIRED_COLUMNS if key.startswith(p)), None)
    result = {"key": key, "valid": True, "issues": [], "rows": 0}
    if not folder:
        result.update(valid=False, issues=["Unknown raw folder - use raw/landsat/, raw/meteorology/ or raw/static/"])
    else:
        try:
            df = pd.read_csv(io.BytesIO(s3.get_object(B, key)))
            result["rows"] = int(len(df))
            missing = [c for c in REQUIRED_COLUMNS[folder] if c not in df.columns]
            if missing:
                result["valid"] = False
                result["issues"].append(f"Missing required columns: {missing}")
            if df.empty:
                result["valid"] = False
                result["issues"].append("File has no rows")
            nulls = df.isna().mean()
            for col, frac in nulls.items():
                if frac > 0.2:
                    result["issues"].append(f"Column {col} is {frac:.0%} empty")
        except Exception as exc:
            result.update(valid=False, issues=[f"Unreadable CSV: {exc}"])

    cloudwatch.put_metric("UHI/DataQuality", "ValidUploads" if result["valid"] else "RejectedUploads", 1)
    context.log(f"Validated {key}: valid={result['valid']} issues={result['issues']}",
                "INFO" if result["valid"] else "WARN")
    if not result["valid"] and folder:
        # Quarantine: move the rejected object out of the raw zone so ETL never reads it
        qkey = "quarantine/" + key
        s3.put_object(B, qkey, s3.get_object(B, key), "text/csv", metadata={"rejected": "; ".join(result["issues"])[:500]},
                      fire_events=False)
        s3.delete_object(B, key)
        result["quarantined_to"] = qkey
        context.log(f"Moved invalid upload to s3://{B}/{qkey}", "WARN")
    if not result["valid"]:
        sns.publish("uhi-pipeline-alerts", "Upload rejected by validation", f"{key}: {'; '.join(result['issues'])}")
    elif SETTINGS["auto_etl_on_upload"] and event.get("auto_etl", True):
        result["glue_job_run"] = glue.start_job_run("uhi-etl-preprocess", {"--trigger": "s3-upload", "--key": key})
        context.log(f"Started Glue job run {result['glue_job_run']}")
    s3.put_object(B, f"logs/validation/{key.replace('/', '_')}.json", json.dumps(result),
                  "application/json", fire_events=False)
    rds.audit("lambda:uhi-validate-upload", "VALIDATE", f"{key} valid={result['valid']}")
    return result


def heat_alert(event, context):
    threshold = float(event.get("threshold", SETTINGS["alert_threshold"]))
    latest = rds.query_one("SELECT MAX(obs_date) d FROM grid_predictions")["d"]
    if not latest:
        return {"alert": False, "reason": "no predictions yet"}
    hot = rds.query("SELECT grid_id, latitude, longitude, uhi_predicted, risk_level FROM grid_predictions "
                    "WHERE obs_date=? AND uhi_predicted>=? ORDER BY uhi_predicted DESC", (latest, threshold))
    cloudwatch.put_metric("UHI/Predictions", "CellsAboveThreshold", len(hot))
    if not hot:
        context.log(f"No cells above {threshold} C on {latest}")
        return {"alert": False, "date": latest, "threshold": threshold, "cells": 0}
    top = "\n".join(f"  - {h['grid_id']} ({h['latitude']:.3f}, {h['longitude']:.3f}): +{h['uhi_predicted']:.2f} C [{h['risk_level']}]"
                    for h in hot[:8])
    msg = (f"Model-based decision-support notice for {config.STUDY_AREA['name']} - observation date {latest}.\n"
           f"{len(hot)} grid cells exceed the configured UHI threshold of +{threshold} C.\n"
           f"Hottest cells:\n{top}\n\nThese are satellite-derived surface-UHI estimates, not air-temperature or medical warnings.")
    mid = sns.publish("uhi-heat-alerts", f"High UHI intensity: {len(hot)} cells >= +{threshold} C", msg,
                      {"date": latest, "cells": len(hot)})
    context.log(f"Published heat alert {mid} for {len(hot)} cells")
    return {"alert": True, "date": latest, "threshold": threshold, "cells": len(hot), "message_id": mid}
