"""
AWS Glue emulation - ETL jobs, job runs and the Glue Data Catalog.

A Glue job is a Python function ``job(args, logger) -> dict``. Runs execute on a
worker thread (like a Glue Spark/Python-shell worker), stream logs to
``/aws-glue/jobs/output`` and record DPU-seconds. The Data Catalog stores table
schemas discovered by the crawler over the S3 processed zone.
"""
import threading
import time
import traceback
import uuid

import pandas as pd

from src import config
from src.aws_cloud import cloudwatch, s3
from src.aws_cloud._jobstore import JobStore

_JOBS = {}
_RUNS = JobStore("glue", "job_runs.json")
_CATALOG = JobStore("glue", "data_catalog.json")
DATABASE = "uhi_geoai_catalog"


def create_job(name, fn, description="", worker_type="G.1X", workers=2, role="UHI-GlueServiceRole"):
    _JOBS[name] = {"Name": name, "_fn": fn, "Description": description, "WorkerType": worker_type,
                   "NumberOfWorkers": workers, "Role": role, "GlueVersion": "4.0",
                   "Command": {"Name": "pythonshell", "PythonVersion": "3.9"}}


def list_jobs():
    out = []
    for j in _JOBS.values():
        runs = [r for r in _RUNS.all() if r["JobName"] == j["Name"]]
        last = max(runs, key=lambda r: r["StartedOn"]) if runs else None
        out.append({**{k: v for k, v in j.items() if not k.startswith("_")},
                    "Runs": len(runs), "LastRunState": last["JobRunState"] if last else "-"})
    return out


def job_runs(name=None):
    runs = _RUNS.all()
    if name:
        runs = [r for r in runs if r["JobName"] == name]
    return sorted(runs, key=lambda r: r["StartedOn"], reverse=True)


def get_job_run(run_id):
    return _RUNS.get(run_id)


def start_job_run(name, arguments=None, wait=False):
    job = _JOBS[name]
    run_id = "jr_" + uuid.uuid4().hex[:20]
    _RUNS.put(run_id, {"Id": run_id, "JobName": name, "Arguments": arguments or {}, "JobRunState": "RUNNING",
                       "StartedOn": cloudwatch.now_iso(), "CompletedOn": None, "ExecutionTime": 0,
                       "DPUSeconds": 0, "ErrorMessage": None, "Output": None})
    t = threading.Thread(target=_execute, args=(job, run_id, arguments or {}), daemon=True)
    t.start()
    if wait:
        t.join()
    return run_id


def _execute(job, run_id, args):
    group = "/aws-glue/jobs/output"
    stream = run_id

    def logger(msg, level="INFO"):
        cloudwatch.log(group, f"[{job['Name']}] {msg}", level, stream)

    start = time.time()
    logger(f"Job run {run_id} started on {job['NumberOfWorkers']} x {job['WorkerType']} workers")
    try:
        output = job["_fn"](args, logger)
        state, err = "SUCCEEDED", None
    except Exception as exc:
        output, state, err = None, "FAILED", f"{exc.__class__.__name__}: {exc}"
        cloudwatch.log("/aws-glue/jobs/error", traceback.format_exc(), "ERROR", stream)
    secs = time.time() - start
    _RUNS.update(run_id, JobRunState=state, CompletedOn=cloudwatch.now_iso(), ExecutionTime=round(secs, 2),
                 DPUSeconds=round(secs * job["NumberOfWorkers"], 2), ErrorMessage=err, Output=output)
    logger(f"Job run {run_id} {state} in {secs:.2f}s" + (f" - {err}" if err else ""), "ERROR" if err else "INFO")
    cloudwatch.put_metric("Glue", "glue.driver.aggregate.elapsedTime", secs * 1000, "Milliseconds", {"JobName": job["Name"]})
    cloudwatch.put_metric("Glue", "JobRunsSucceeded" if state == "SUCCEEDED" else "JobRunsFailed", 1, dimensions={"JobName": job["Name"]})


# ---------------------------------------------------------- Data Catalog ------
def run_crawler(prefix="processed/"):
    """Crawl CSV objects under an S3 prefix and register/refresh their schemas."""
    tables = []
    for obj in s3.list_objects(config.S3_BUCKET, prefix):
        if not obj["Key"].endswith(".csv"):
            continue
        df = pd.read_csv(s3.local_path(config.S3_BUCKET, obj["Key"]), nrows=200)
        name = obj["Key"].split("/")[-1].replace(".csv", "").replace("-", "_")
        cols = [{"Name": c, "Type": _glue_type(str(t))} for c, t in df.dtypes.items()]
        table = {"Name": name, "DatabaseName": DATABASE, "Location": f"s3://{config.S3_BUCKET}/{obj['Key']}",
                 "Columns": cols, "SizeBytes": obj["Size"], "UpdateTime": cloudwatch.now_iso(),
                 "Classification": "csv"}
        _CATALOG.put(name, table)
        tables.append(name)
    cloudwatch.log("/aws-glue/crawlers", f"Crawler uhi-processed-crawler updated {len(tables)} table(s): {tables}")
    return tables


def _glue_type(t):
    return {"int64": "bigint", "float64": "double", "object": "string", "bool": "boolean"}.get(t, "string")


def catalog_tables():
    return _CATALOG.all()
