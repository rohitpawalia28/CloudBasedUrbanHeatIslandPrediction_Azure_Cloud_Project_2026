"""
Amazon SageMaker emulation - training jobs, model registry, real-time endpoint and
batch transform.

* Training job : reads s3://<bucket>/processed/uhi_model_ready.csv, runs
                 ``src.ai_model.trainer.train`` on a worker thread and uploads
                 model.joblib + metrics.json to model-artifacts/<job>/output/.
* Model registry: every completed job is registered as a model package in the
                 'UHI-Intensity-Models' group (PendingManualApproval -> Approved).
* Endpoint      : 'uhi-intensity-endpoint' hosts one approved model package in memory
                 and serves InvokeEndpoint requests (with latency metrics).
* Batch transform: scores every grid cell and writes predictions to S3 + RDS.
"""
import io
import json
import threading
import time
import traceback
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from src import config
from src.aws_cloud import cloudwatch, rds, s3
from src.aws_cloud._jobstore import JobStore

B = config.S3_BUCKET
MODEL_GROUP = "UHI-Intensity-Models"
ENDPOINT_NAME = "uhi-intensity-endpoint"
_JOBS = JobStore("sagemaker", "training_jobs.json")
_PACKAGES = JobStore("sagemaker", "model_packages.json")
_ENDPOINTS = JobStore("sagemaker", "endpoints.json")
_MODEL_CACHE = {}
_LOCK = threading.RLock()


def _arn(kind, name):
    return f"arn:aws:sagemaker:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:{kind}/{name}"


# ------------------------------------------------------------- Training -------
def create_training_job(algorithm, hyperparameters=None, instance_type="ml.m5.xlarge", wait=False, created_by="system"):
    from src.ai_model.trainer import ALGORITHMS
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm '{algorithm}'")
    if not s3.exists(B, "processed/uhi_model_ready.csv"):
        raise RuntimeError("Processed dataset not found - run the Glue ETL job first")
    name = f"uhi-{algorithm.replace('_', '-')}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}"
    _JOBS.put(name, {
        "TrainingJobName": name, "TrainingJobArn": _arn("training-job", name), "Algorithm": algorithm,
        "AlgorithmLabel": ALGORITHMS[algorithm]["label"], "TrainingImage": ALGORITHMS[algorithm]["image"],
        "HyperParameters": {**ALGORITHMS[algorithm]["defaults"], **(hyperparameters or {})},
        "ResourceConfig": {"InstanceType": instance_type, "InstanceCount": 1, "VolumeSizeInGB": 30},
        "InputDataConfig": f"s3://{B}/processed/uhi_model_ready.csv",
        "OutputDataConfig": f"s3://{B}/model-artifacts/{name}/output/",
        "RoleArn": f"arn:aws:iam::{config.AWS_ACCOUNT_ID}:role/UHI-SageMakerExecutionRole",
        "TrainingJobStatus": "InProgress", "SecondaryStatus": "Starting",
        "CreationTime": cloudwatch.now_iso(), "TrainingEndTime": None, "BillableTimeInSeconds": 0,
        "FinalMetrics": None, "FailureReason": None, "CreatedBy": created_by,
    })
    t = threading.Thread(target=_run_training, args=(name, algorithm, hyperparameters or {}), daemon=True)
    t.start()
    if wait:
        t.join()
    return name


def _run_training(name, algorithm, hp):
    from src.ai_model import trainer
    group, stream = "/aws/sagemaker/TrainingJobs", f"{name}/algo-1"

    def log(msg, level="INFO"):
        cloudwatch.log(group, msg, level, stream)

    start = time.time()
    try:
        for status in ("Starting", "Downloading"):
            _JOBS.update(name, SecondaryStatus=status)
            log(f"{status}: preparing {_JOBS.get(name)['ResourceConfig']['InstanceType']} instance")
            time.sleep(0.4)
        df = pd.read_csv(s3.local_path(B, "processed/uhi_model_ready.csv"))
        log(f"Downloaded training data: {len(df)} rows from s3://{B}/processed/uhi_model_ready.csv")
        _JOBS.update(name, SecondaryStatus="Training")
        model, report = trainer.train(df, algorithm, hp, log=log)
        _JOBS.update(name, SecondaryStatus="Uploading")
        buf = io.BytesIO()
        joblib.dump({"model": model, "features": config.FEATURES, "algorithm": algorithm, "job": name}, buf, compress=3)
        s3.put_object(B, f"model-artifacts/{name}/output/model.joblib", buf.getvalue(),
                      "application/octet-stream", fire_events=False)
        s3.put_object(B, f"model-artifacts/{name}/output/metrics.json", json.dumps(report, indent=2),
                      "application/json", fire_events=False)
        secs = round(time.time() - start, 2)
        _JOBS.update(name, TrainingJobStatus="Completed", SecondaryStatus="Completed",
                     TrainingEndTime=cloudwatch.now_iso(), BillableTimeInSeconds=secs,
                     FinalMetrics=report["metrics"], FeatureImportance=report["feature_importance"],
                     Scatter=report["scatter"], HyperParameters=report["hyperparameters"])
        for k in ("MAE", "RMSE", "R2"):
            cloudwatch.put_metric("/aws/sagemaker/TrainingJobs", f"validation:{k}", report["metrics"][k],
                                  "None", {"TrainingJobName": name})
        register_model(name)
        log(f"Training job {name} completed in {secs}s")
    except Exception as exc:
        log(traceback.format_exc(), "ERROR")
        _JOBS.update(name, TrainingJobStatus="Failed", SecondaryStatus="Failed",
                     FailureReason=f"{exc.__class__.__name__}: {exc}", TrainingEndTime=cloudwatch.now_iso())


def list_training_jobs():
    return sorted(_JOBS.all(), key=lambda j: j["CreationTime"], reverse=True)


def describe_training_job(name):
    return _JOBS.get(name)


# --------------------------------------------------------- Model registry -----
def register_model(job_name):
    job = _JOBS.get(job_name)
    version = len(_PACKAGES.all()) + 1
    pkg = {
        "ModelPackageArn": _arn("model-package", f"{MODEL_GROUP.lower()}/{version}"),
        "ModelPackageGroupName": MODEL_GROUP, "ModelPackageVersion": version,
        "TrainingJobName": job_name, "Algorithm": job["Algorithm"], "AlgorithmLabel": job["AlgorithmLabel"],
        "ModelDataUrl": f"s3://{B}/model-artifacts/{job_name}/output/model.joblib",
        "Metrics": job["FinalMetrics"], "ModelApprovalStatus": "PendingManualApproval",
        "CreationTime": cloudwatch.now_iso(),
    }
    _PACKAGES.put(str(version), pkg)
    cloudwatch.log("/aws/sagemaker/ModelRegistry", f"Registered {MODEL_GROUP} v{version} from {job_name} (R2={job['FinalMetrics']['R2']})")
    return pkg


def list_model_packages():
    return sorted(_PACKAGES.all(), key=lambda p: p["ModelPackageVersion"], reverse=True)


def update_approval(version, status):
    if status not in ("Approved", "Rejected", "PendingManualApproval"):
        raise ValueError("Invalid approval status")
    pkg = _PACKAGES.update(str(version), ModelApprovalStatus=status)
    cloudwatch.log("/aws/sagemaker/ModelRegistry", f"{MODEL_GROUP} v{version} -> {status}")
    return pkg


def best_package():
    pk = [p for p in _PACKAGES.all() if p.get("Metrics")]
    return max(pk, key=lambda p: p["Metrics"]["R2"]) if pk else None


# --------------------------------------------------------------- Endpoint -----
def deploy(version, instance_type="ml.t2.medium"):
    pkg = _PACKAGES.get(str(version))
    if not pkg:
        raise ValueError(f"Model package v{version} not found")
    if pkg["ModelApprovalStatus"] != "Approved":
        update_approval(version, "Approved")
    _ENDPOINTS.put(ENDPOINT_NAME, {
        "EndpointName": ENDPOINT_NAME, "EndpointArn": _arn("endpoint", ENDPOINT_NAME),
        "EndpointStatus": "Creating", "ModelPackageVersion": int(version), "Algorithm": pkg["Algorithm"],
        "AlgorithmLabel": pkg["AlgorithmLabel"], "ModelDataUrl": pkg["ModelDataUrl"], "Metrics": pkg["Metrics"],
        "ProductionVariants": [{"VariantName": "AllTraffic", "InstanceType": instance_type,
                                "InitialInstanceCount": 1, "DataCaptureEnabled": True}],
        "CreationTime": cloudwatch.now_iso(), "LastModifiedTime": cloudwatch.now_iso(),
    })
    _load_model(pkg)
    _ENDPOINTS.update(ENDPOINT_NAME, EndpointStatus="InService", LastModifiedTime=cloudwatch.now_iso())
    cloudwatch.log("/aws/sagemaker/Endpoints/" + ENDPOINT_NAME, f"Endpoint InService with {MODEL_GROUP} v{version} ({pkg['Algorithm']})")
    return _ENDPOINTS.get(ENDPOINT_NAME)


def _load_model(pkg):
    key = pkg["ModelDataUrl"].replace(f"s3://{B}/", "")
    with _LOCK:
        _MODEL_CACHE[ENDPOINT_NAME] = {"bundle": joblib.load(s3.local_path(B, key)),
                                       "version": f"v{pkg['ModelPackageVersion']}-{pkg['Algorithm']}"}


def describe_endpoint():
    ep = _ENDPOINTS.get(ENDPOINT_NAME)
    if ep and ep["EndpointStatus"] == "InService" and ENDPOINT_NAME not in _MODEL_CACHE:
        pkg = _PACKAGES.get(str(ep["ModelPackageVersion"]))
        if pkg:
            _load_model(pkg)
    return ep


def endpoint_model_version():
    describe_endpoint()
    m = _MODEL_CACHE.get(ENDPOINT_NAME)
    return m["version"] if m else None


def invoke_endpoint(records):
    """records: list of dicts with every feature in config.FEATURES. Returns list of floats."""
    if not describe_endpoint():
        raise RuntimeError("No SageMaker endpoint is InService - train and deploy a model first")
    start = time.time()
    df = pd.DataFrame(records)
    missing = [f for f in config.FEATURES if f not in df.columns]
    if missing:
        raise ValueError(f"Missing features: {missing}")
    df = df[config.FEATURES].astype(float)
    preds = _MODEL_CACHE[ENDPOINT_NAME]["bundle"]["model"].predict(df)
    latency = (time.time() - start) * 1000
    dims = {"EndpointName": ENDPOINT_NAME, "VariantName": "AllTraffic"}
    cloudwatch.put_metric("AWS/SageMaker", "Invocations", 1, dimensions=dims)
    cloudwatch.put_metric("AWS/SageMaker", "ModelLatency", latency, "Milliseconds", dims)
    cloudwatch.put_metric("UHI/Predictions", "MaxPredictedUHI", float(np.max(preds)), "None")
    return [round(float(p), 3) for p in preds]


# --------------------------------------------------------- Batch transform ----
def batch_transform(log=print):
    df = pd.read_csv(s3.local_path(B, "processed/uhi_model_ready.csv"))
    preds = np.array(invoke_endpoint(df[config.FEATURES].to_dict("records")))
    df["uhi_predicted"] = preds.round(3)
    df["risk_level"] = [config.risk_level(v) for v in preds]
    version = endpoint_model_version()

    out = df[["grid_id", "timestamp", "latitude", "longitude", "lst_celsius", "ndvi", "ndbi", "lulc_code",
              "uhi_intensity_celsius", "uhi_predicted", "risk_level"]]
    buf = io.StringIO()
    out.to_csv(buf, index=False)
    s3.put_object(B, "predictions/uhi_grid_predictions.csv", buf.getvalue(), "text/csv", fire_events=False)

    geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r.longitude, r.latitude]},
         "properties": {"grid_id": r.grid_id, "date": r.timestamp, "uhi_predicted": r.uhi_predicted,
                        "risk": r.risk_level}}
        for r in out[out["timestamp"] == out["timestamp"].max()].itertuples()]}
    s3.put_object(B, "predictions/uhi_latest_hotspots.geojson", json.dumps(geo), "application/geo+json", fire_events=False)

    rds.execute("DELETE FROM grid_predictions")
    rds.executemany("INSERT INTO grid_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r.grid_id, r.timestamp, r.latitude, r.longitude, r.lst_celsius, r.ndvi, r.ndbi, int(r.lulc_code),
                      r.uhi_intensity_celsius, r.uhi_predicted, r.risk_level, version) for r in out.itertuples()])
    rds.execute("DELETE FROM hotspot_summaries")
    now = cloudwatch.now_iso()
    for (date, cls), g in df.groupby(["timestamp", "lulc_class"]):
        rds.execute("INSERT INTO hotspot_summaries (obs_date, zone, cells, mean_uhi, max_uhi, mean_ndvi, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (date, cls, len(g), round(g["uhi_predicted"].mean(), 3),
                                              round(g["uhi_predicted"].max(), 3), round(g["ndvi"].mean(), 3), now))
    log(f"Batch transform scored {len(df)} cells with model {version}")
    return {"rows": int(len(df)), "model_version": version,
            "severe_cells": int((df["risk_level"] == "Severe").sum()),
            "max_uhi": round(float(preds.max()), 2), "output": f"s3://{B}/predictions/uhi_grid_predictions.csv"}
