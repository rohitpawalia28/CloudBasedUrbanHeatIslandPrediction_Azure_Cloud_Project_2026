"""
Optional bridge to a real AWS account (CLOUD_MODE=aws).

When enabled, every object written to the emulated S3 data lake is also uploaded to
the real S3 bucket, and every SNS publish is also sent to the real SNS topic. The
local emulation keeps running, so the dashboard behaves the same either way.

Credentials come from the standard AWS chain (``aws configure``, environment
variables or an instance role). Nothing is hard-coded.
"""
import threading

from src import config

_client_cache = {}
_lock = threading.Lock()
_status = {"enabled": config.CLOUD_MODE == "aws", "error": None, "s3_uploads": 0, "sns_publishes": 0}


def _client(name):
    if name not in _client_cache:
        import boto3  # imported lazily so boto3 is only needed in aws mode
        _client_cache[name] = boto3.client(name, region_name=config.AWS_REGION)
    return _client_cache[name]


def status():
    return dict(_status, mode=config.CLOUD_MODE, region=config.AWS_REGION, bucket=config.S3_BUCKET)


def mirror_put(bucket, key, body, content_type):
    if not _status["enabled"]:
        return
    try:
        _client("s3").put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type,
                                 ServerSideEncryption="AES256")
        with _lock:
            _status["s3_uploads"] += 1
    except Exception as exc:  # never break the local platform because AWS is unreachable
        _status["error"] = f"S3: {exc}"


def mirror_publish(topic, subject, message):
    if not _status["enabled"]:
        return
    try:
        sns = _client("sns")
        arn = sns.create_topic(Name=topic)["TopicArn"]
        sns.publish(TopicArn=arn, Subject=subject[:99], Message=message)
        with _lock:
            _status["sns_publishes"] += 1
    except Exception as exc:
        _status["error"] = f"SNS: {exc}"
