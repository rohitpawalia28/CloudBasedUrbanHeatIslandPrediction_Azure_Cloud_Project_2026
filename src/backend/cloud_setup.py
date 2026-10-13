"""
Infrastructure provisioning for the emulated AWS account (the local equivalent of
``terraform apply`` in infrastructure/terraform) and the Step Functions definition of
the end-to-end UHI pipeline.
"""
import threading

from src import config
from src.aws_cloud import (api_gateway, cloudwatch, cognito, glue, lambda_service, rds, s3, sagemaker, sns,
                           step_functions)
from src.ai_model import preprocessing
from src.lambdas import handlers

PIPELINE = "UHI-End-to-End-Pipeline"
B = config.S3_BUCKET
_provisioned = False
_lock = threading.Lock()


# --------------------------------------------------------- pipeline states ----
def _ingest(data):
    r = lambda_service.invoke("uhi-ingest-raw-data", {"force": bool(data.get("input", {}).get("regenerate"))})
    if r["FunctionError"]:
        raise RuntimeError(r["FunctionError"])
    return r["Payload"]


def _validate(data):
    results = []
    for obj in s3.list_objects(B, "raw/"):
        if not obj["Key"].endswith(".csv"):
            continue
        ev = {"Records": [{"s3": {"bucket": {"name": B}, "object": {"key": obj["Key"], "size": obj["Size"]}}}],
              "auto_etl": False}
        r = lambda_service.invoke("uhi-validate-upload", ev)["Payload"]
        results.append({"key": r["key"], "valid": r["valid"], "rows": r["rows"]})
    bad = [r for r in results if not r["valid"]]
    if bad:
        raise RuntimeError(f"{len(bad)} raw file(s) failed validation: {[b['key'] for b in bad]}")
    return {"validated": len(results), "files": results}


def _etl(data):
    run_id = glue.start_job_run("uhi-etl-preprocess", {"--trigger": "step-functions"}, wait=True)
    run = glue.get_job_run(run_id)
    if run["JobRunState"] != "SUCCEEDED":
        raise RuntimeError(run["ErrorMessage"])
    return {"jobRunId": run_id, "rows": run["Output"]["rows"], "output": run["Output"]["output"]}


def _crawl(data):
    return {"tables": glue.run_crawler("processed/"), "database": glue.DATABASE}


def _train(algorithm):
    def fn(data):
        hp = data.get("input", {}).get("hyperparameters", {}).get(algorithm, {})
        name = sagemaker.create_training_job(algorithm, hp, wait=True,
                                             created_by=data.get("input", {}).get("startedBy", "pipeline"))
        job = sagemaker.describe_training_job(name)
        if job["TrainingJobStatus"] != "Completed":
            raise RuntimeError(job["FailureReason"])
        return {"job": name, **job["FinalMetrics"]}
    fn.__name__ = f"train_{algorithm}"
    return fn


def _select_best(data):
    jobs = list(data["TrainModels"].values())
    best = max(jobs, key=lambda j: j["R2"])
    pkg = next(p for p in sagemaker.list_model_packages() if p["TrainingJobName"] == best["job"])
    sagemaker.update_approval(pkg["ModelPackageVersion"], "Approved")
    for j in jobs:
        if j["job"] != best["job"]:
            other = next(p for p in sagemaker.list_model_packages() if p["TrainingJobName"] == j["job"])
            sagemaker.update_approval(other["ModelPackageVersion"], "Rejected")
    return {"bestJob": best["job"], "modelPackageVersion": pkg["ModelPackageVersion"],
            "R2": best["R2"], "RMSE": best["RMSE"], "MAE": best["MAE"]}


def _deploy(data):
    ep = sagemaker.deploy(data["SelectBestModel"]["modelPackageVersion"])
    return {"endpoint": ep["EndpointName"], "status": ep["EndpointStatus"], "modelVersion": ep["ModelPackageVersion"]}


def _batch(data):
    return sagemaker.batch_transform(log=lambda m: cloudwatch.log("/aws/sagemaker/TransformJobs", m))


def _alerts(data):
    return lambda_service.invoke("uhi-heat-alert", {})["Payload"]


def _record(data):
    import json
    manifest = json.loads(s3.get_object(B, "processed/dataset_manifest.json"))
    head = s3.head_object(B, "processed/uhi_model_ready.csv")
    count = rds.query_one("SELECT COUNT(*) n FROM datasets")["n"]
    rds.execute("INSERT INTO datasets (name, version, s3_uri, source, rows, columns, size_bytes, study_area, created_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("uhi_model_ready", f"v{count + 1}", f"s3://{B}/processed/uhi_model_ready.csv",
                 "Landsat L2 indices + station meteorology + LULC/DEM", manifest["rows"], manifest["columns"],
                 head["Size"], manifest["study_area"], cloudwatch.now_iso(), "APPROVED"))
    snap = rds.snapshot()
    return {"datasetVersion": f"v{count + 1}", "rdsSnapshot": snap}


# ------------------------------------------------------------- provision ------
def provision():
    global _provisioned
    with _lock:
        if _provisioned:
            return
        cloudwatch.init()
        rds.init()
        sns.init()
        cognito.init()
        api_gateway.init()
        s3.create_bucket(B)

        sns.create_topic("uhi-heat-alerts", "UHI High-Heat Alerts")
        sns.create_topic("uhi-pipeline-alerts", "UHI Pipeline Failures")
        for t in ("uhi-heat-alerts", "uhi-pipeline-alerts"):
            sns.subscribe(t, "app", "console-notification-center")
            sns.subscribe(t, "email", config.ADMIN_EMAIL)

        cloudwatch.put_alarm("UHI-Lambda-Errors", "AWS/Lambda", "Errors", "GreaterThanThreshold", 0, 300, "Sum",
                             "uhi-pipeline-alerts")
        cloudwatch.put_alarm("UHI-API-5XX", "AWS/ApiGateway", "5XXError", "GreaterThanOrEqualToThreshold", 5, 300, "Sum",
                             "uhi-pipeline-alerts")
        cloudwatch.put_alarm("UHI-Extreme-Heat-Prediction", "UHI/Predictions", "MaxPredictedUHI",
                             "GreaterThanThreshold", 9.0, 300, "Maximum", "uhi-heat-alerts")
        cloudwatch.put_alarm("UHI-Rejected-Uploads", "UHI/DataQuality", "RejectedUploads", "GreaterThanThreshold",
                             0, 300, "Sum", "uhi-pipeline-alerts")

        lambda_service.create_function("uhi-ingest-raw-data", handlers.ingest_raw_data,
                                       "Lands Landsat, meteorology and static layers in the S3 raw zone", 1024, 300)
        lambda_service.create_function("uhi-validate-upload", handlers.validate_upload,
                                       "Schema/quality validation for raw uploads; starts Glue ETL", 512, 120,
                                       trigger=f"S3 ObjectCreated: s3://{B}/raw/*.csv")
        lambda_service.create_function("uhi-heat-alert", handlers.heat_alert,
                                       "Publishes SNS alerts for cells above the UHI threshold", 256, 60,
                                       trigger="Step Functions / manual")
        s3.put_bucket_notification(
            B, lambda ev: lambda_service.invoke("uhi-validate-upload", ev, "Event"),
            prefix="raw/", suffix=".csv", name="raw-upload-to-validate-lambda")

        glue.create_job("uhi-etl-preprocess", preprocessing.run_etl,
                        "Cloud-mask, impute, IDW weather, join layers, compute UHI target", "G.1X", 2)

        step_functions.create_state_machine(PIPELINE, [
            {"Name": "IngestRawData", "Type": "Task", "Resource": "lambda:uhi-ingest-raw-data", "Fn": _ingest, "Retry": 1},
            {"Name": "ValidateRawData", "Type": "Task", "Resource": "lambda:uhi-validate-upload", "Fn": _validate},
            {"Name": "GlueETL", "Type": "Task", "Resource": "glue:startJobRun.sync", "Fn": _etl, "Retry": 1},
            {"Name": "CatalogCrawler", "Type": "Task", "Resource": "glue:startCrawler", "Fn": _crawl},
            {"Name": "TrainModels", "Type": "Parallel", "Resource": "sagemaker:createTrainingJob.sync",
             "Branches": [("linear_regression", _train("linear_regression")),
                          ("random_forest", _train("random_forest")),
                          ("gradient_boosting", _train("gradient_boosting"))]},
            {"Name": "SelectBestModel", "Type": "Task", "Resource": "sagemaker:updateModelPackage", "Fn": _select_best},
            {"Name": "DeployEndpoint", "Type": "Task", "Resource": "sagemaker:createEndpoint", "Fn": _deploy},
            {"Name": "BatchTransform", "Type": "Task", "Resource": "sagemaker:createTransformJob.sync", "Fn": _batch},
            {"Name": "HeatAlertCheck", "Type": "Task", "Resource": "lambda:uhi-heat-alert", "Fn": _alerts},
            {"Name": "RecordMetadata", "Type": "Task", "Resource": "rds-data:executeStatement", "Fn": _record},
        ], comment="Ingest -> validate -> ETL -> train x3 -> select -> deploy -> batch predict -> alert -> record")

        _recover()
        _provisioned = True
        cloudwatch.log("/uhi/platform", f"Infrastructure provisioned in {config.AWS_REGION} (mode={config.CLOUD_MODE})")


def _recover():
    step_functions.recover_interrupted()
    for run in glue.job_runs():
        if run["JobRunState"] == "RUNNING":
            glue._RUNS.update(run["Id"], JobRunState="STOPPED", ErrorMessage="Server restarted")
    for job in sagemaker.list_training_jobs():
        if job["TrainingJobStatus"] == "InProgress":
            sagemaker._JOBS.update(job["TrainingJobName"], TrainingJobStatus="Stopped", SecondaryStatus="Stopped",
                                   FailureReason="Server restarted")


def run_pipeline(started_by="system", regenerate=False, wait=False):
    if step_functions.running():
        raise RuntimeError("A pipeline execution is already running")
    return step_functions.start_execution(PIPELINE, {"startedBy": started_by, "regenerate": regenerate},
                                          started_by, wait=wait)


def needs_first_run():
    return sagemaker.describe_endpoint() is None and not step_functions.running()
