"""
Bridge to a REAL AWS account (CLOUD_MODE=aws).

The emulated services keep running (so the console behaves the same either way) and, in
addition, this module mirrors platform activity to real AWS services:

  Amazon S3          every object written to the data lake          -> real bucket
  Amazon SNS         every alert published                          -> real topic (e-mail)
  Amazon DynamoDB    every prediction request                       -> table uhi-predictions
  CloudWatch Logs    platform log events (batched every few seconds) -> log group /uhi/platform
  CloudWatch metrics custom UHI/* metrics                           -> real metrics / alarms
  AWS Lambda         invokes the heat-alert checker on demand
  SSM Parameter Store resource names are read from /uhi/config at start-up

Resource names come from SSM (created by infrastructure/cloudformation/uhi-aws-stack.yaml) and can be
overridden with UHI_AWS_BUCKET, UHI_AWS_TOPIC_ARN, UHI_AWS_TABLE, UHI_AWS_LOG_GROUP, UHI_AWS_LAMBDA.
Credentials come from the standard AWS chain (EC2 instance role, ``aws configure`` or environment
variables). Nothing is hard-coded, and an AWS failure never breaks the local platform.
"""
import json
import os
import re
import threading
import time
import uuid
from decimal import Decimal

from src import config

_client_cache = {}
_lock = threading.Lock()
_queue_lock = threading.Lock()
_log_queue, _metric_queue = [], []
_streams = set()
_flusher = None
_res = None
_status = {"enabled": config.CLOUD_MODE == "aws", "error": None, "config_source": None,
           "s3_uploads": 0, "sns_publishes": 0, "dynamodb_writes": 0, "log_events": 0,
           "metric_points": 0, "lambda_invocations": 0}


def _client(name):
    if name not in _client_cache:
        import boto3  # imported lazily so boto3 is only needed in aws mode
        _client_cache[name] = boto3.client(name, region_name=config.AWS_REGION)
    return _client_cache[name]


def _count(key, n=1):
    with _lock:
        _status[key] += n


def _fail(service, exc):
    _status["error"] = f"{service}: {exc}"[:300]


def resources():
    """Real resource names: SSM /uhi/config, overridden by UHI_AWS_* environment variables."""
    global _res
    if _res is not None:
        return _res
    res = {}
    if _status["enabled"]:
        try:
            raw = _client("ssm").get_parameter(Name=os.getenv("UHI_AWS_SSM_PARAM", "/uhi/config"))["Parameter"]["Value"]
            res = json.loads(raw)
            _status["config_source"] = "SSM Parameter Store /uhi/config"
        except Exception as exc:
            _fail("SSM", exc)
            _status["config_source"] = "environment / defaults"
    for key, env in (("bucket", "UHI_AWS_BUCKET"), ("topic_arn", "UHI_AWS_TOPIC_ARN"), ("table", "UHI_AWS_TABLE"),
                     ("log_group", "UHI_AWS_LOG_GROUP"), ("lambda", "UHI_AWS_LAMBDA")):
        if os.getenv(env):
            res[key] = os.getenv(env)
    res.setdefault("bucket", config.S3_BUCKET)
    res.setdefault("log_group", "/uhi/platform")
    _res = res
    return res


def status():
    res = resources() if _status["enabled"] else {}
    return dict(_status, mode=config.CLOUD_MODE, region=config.AWS_REGION, resources=res)


# ----------------------------------------------------------------- S3 ------
def mirror_put(bucket, key, body, content_type):
    if not _status["enabled"]:
        return
    try:
        _client("s3").put_object(Bucket=resources()["bucket"], Key=key, Body=body, ContentType=content_type,
                                 ServerSideEncryption="AES256")
        _count("s3_uploads")
    except Exception as exc:  # never break the local platform because AWS is unreachable
        _fail("S3", exc)


# ----------------------------------------------------------------- SNS -----
def mirror_publish(topic, subject, message):
    if not _status["enabled"]:
        return
    try:
        sns = _client("sns")
        arn = resources().get("topic_arn") or sns.create_topic(Name=topic)["TopicArn"]
        sns.publish(TopicArn=arn, Subject=f"[{topic}] {subject}"[:99], Message=message)
        _count("sns_publishes")
    except Exception as exc:
        _fail("SNS", exc)


# ------------------------------------------------------------ DynamoDB -----
def _ddb_value(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return Decimal(str(round(v, 6)))
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, dict):
        return {k: _ddb_value(x) for k, x in v.items()}
    return v


def mirror_prediction(record: dict):
    """Store one prediction request in DynamoDB (asynchronously, off the request path)."""
    if not _status["enabled"] or not resources().get("table"):
        return

    def put():
        try:
            import boto3
            table = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(resources()["table"])
            item = {k: _ddb_value(v) for k, v in record.items() if v is not None}
            item["request_id"] = str(uuid.uuid4())
            table.put_item(Item=item)
            _count("dynamodb_writes")
        except Exception as exc:
            _fail("DynamoDB", exc)
    threading.Thread(target=put, daemon=True).start()


# ------------------------------------------------------------- Lambda ------
def invoke_heat_checker():
    """Synchronously invoke the real heat-alert Lambda; returns its JSON result."""
    if not _status["enabled"]:
        raise RuntimeError("CLOUD_MODE is not 'aws' - real Lambda is not connected")
    name = resources().get("lambda", "uhi-heat-alert-checker")
    resp = _client("lambda").invoke(FunctionName=name, InvocationType="RequestResponse", Payload=b"{}")
    _count("lambda_invocations")
    payload = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {payload}")
    return payload


# ------------------------------------------------- CloudWatch (batched) ----
def mirror_log(group, message, level="INFO"):
    if not _status["enabled"]:
        return
    with _queue_lock:
        if len(_log_queue) < 5000:
            _log_queue.append((int(time.time() * 1000), group, f"[{level}] {message}"[:8000]))
    _ensure_flusher()


def mirror_metric(namespace, name, value, unit="Count", dimensions=None):
    # only the project's own metrics (a handful of names) go to real CloudWatch, to stay in the free tier
    if not _status["enabled"] or not str(namespace).startswith("UHI/"):
        return
    unit = unit if unit in ("Count", "Seconds", "Milliseconds", "Percent", "Bytes", "None") else "None"
    with _queue_lock:
        _metric_queue.append((namespace, {"MetricName": name, "Value": float(value), "Unit": unit,
                                          "Dimensions": [{"Name": k, "Value": str(v)} for k, v in (dimensions or {}).items()][:10]}))
    _ensure_flusher()


def _ensure_flusher():
    global _flusher
    if _flusher is None or not _flusher.is_alive():
        _flusher = threading.Thread(target=_flush_loop, daemon=True, name="aws-cloudwatch-flusher")
        _flusher.start()


def _stream_name(group):
    return (re.sub(r"[:*]", "_", group).strip("/") or "platform")[:500]


def _flush_loop():
    while True:
        time.sleep(5)
        try:
            _flush()
        except Exception as exc:
            _fail("CloudWatch", exc)


def _flush():
    with _queue_lock:
        logs, metrics = _log_queue[:], _metric_queue[:]
        _log_queue.clear()
        _metric_queue.clear()
    if logs:
        cwl, group = _client("logs"), resources()["log_group"]
        by_stream = {}
        for ts, g, msg in logs:
            by_stream.setdefault(_stream_name(g), []).append({"timestamp": ts, "message": msg})
        for stream, events in by_stream.items():
            if stream not in _streams:
                try:
                    cwl.create_log_stream(logGroupName=group, logStreamName=stream)
                except cwl.exceptions.ResourceAlreadyExistsException:
                    pass
                _streams.add(stream)
            events.sort(key=lambda e: e["timestamp"])
            for i in range(0, len(events), 500):
                cwl.put_log_events(logGroupName=group, logStreamName=stream, logEvents=events[i:i + 500])
        _count("log_events", len(logs))
    if metrics:
        cw = _client("cloudwatch")
        by_ns = {}
        for ns, datum in metrics:
            by_ns.setdefault(ns, []).append(datum)
        for ns, data in by_ns.items():
            for i in range(0, len(data), 500):
                cw.put_metric_data(Namespace=ns, MetricData=data[i:i + 500])
        _count("metric_points", len(metrics))
