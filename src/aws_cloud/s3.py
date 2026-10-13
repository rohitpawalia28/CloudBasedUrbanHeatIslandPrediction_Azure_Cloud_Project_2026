"""
Amazon S3 emulation - the project data lake.

Objects live on disk under ``cloud_runtime/s3/<bucket>/<key>``. Each object keeps
S3-style metadata (ETag, size, content type, version id, server-side encryption,
storage class, tags). Bucket versioning, block-public-access, lifecycle rules and
event notifications (ObjectCreated -> Lambda) are supported.
"""
import hashlib
import hmac
from urllib.parse import quote
import json
import mimetypes
import shutil
import threading
import time
import uuid
from pathlib import Path

from src import config
from src.aws_cloud import cloudwatch

_LOCK = threading.RLock()
_NOTIFICATIONS = {}   # bucket -> list of {prefix, suffix, events, handler}

NS = "AWS/S3"


class NoSuchKey(Exception):
    pass


def _bucket_dir(bucket):
    return config.S3_ROOT / bucket


def _meta_path(bucket, key):
    return config.S3_ROOT / ".meta" / bucket / (key + ".json")


def _bucket_cfg_path(bucket):
    return config.S3_ROOT / ".meta" / bucket / "_bucket.json"


def _safe_key(key: str) -> str:
    key = key.replace("\\", "/").lstrip("/")
    if ".." in Path(key).parts:
        raise ValueError("Invalid S3 key")
    return key


# ------------------------------------------------------------ Buckets ---------
def create_bucket(bucket, versioning=True):
    with _LOCK:
        _bucket_dir(bucket).mkdir(parents=True, exist_ok=True)
        cfg_path = _bucket_cfg_path(bucket)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if not cfg_path.exists():
            cfg = {
                "Name": bucket, "Region": config.AWS_REGION,
                "CreationDate": cloudwatch.now_iso(),
                "Versioning": "Enabled" if versioning else "Suspended",
                "Encryption": {"SSEAlgorithm": "AES256"},
                "PublicAccessBlock": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                      "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
                "Lifecycle": [
                    {"ID": "expire-logs", "Prefix": "logs/", "ExpirationDays": 30},
                    {"ID": "raw-to-glacier", "Prefix": "raw/", "TransitionDays": 90, "StorageClass": "GLACIER"},
                ],
            }
            cfg_path.write_text(json.dumps(cfg, indent=2))
            cloudwatch.log("/aws/s3/" + bucket, f"Bucket {bucket} created in {config.AWS_REGION}")
    return bucket_config(bucket)


def bucket_config(bucket):
    p = _bucket_cfg_path(bucket)
    return json.loads(p.read_text()) if p.exists() else None


def list_buckets():
    if not config.S3_ROOT.exists():
        return []
    out = []
    for d in sorted(config.S3_ROOT.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            objs = list_objects(d.name)
            out.append({**(bucket_config(d.name) or {"Name": d.name}),
                        "ObjectCount": len(objs), "TotalSize": sum(o["Size"] for o in objs)})
    return out


# ------------------------------------------------------------ Objects ---------
def put_object(bucket, key, body, content_type=None, metadata=None, tags=None, fire_events=True):
    key = _safe_key(key)
    if isinstance(body, str):
        body = body.encode("utf-8")
    start = time.time()
    with _LOCK:
        create_bucket(bucket)
        path = _bucket_dir(bucket) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_path = _meta_path(bucket, key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        # bucket versioning - keep previous version
        if path.exists() and meta_path.exists():
            old = json.loads(meta_path.read_text())
            vdir = config.S3_ROOT / ".versions" / bucket / key
            vdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, vdir / old["VersionId"])
        path.write_bytes(body)
        meta = {
            "Key": key, "Size": len(body), "ETag": hashlib.md5(body).hexdigest(),
            "ContentType": content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
            "LastModified": cloudwatch.now_iso(), "VersionId": uuid.uuid4().hex[:16],
            "ServerSideEncryption": "AES256", "StorageClass": "STANDARD",
            "Metadata": metadata or {}, "Tags": tags or {},
        }
        meta_path.write_text(json.dumps(meta, indent=2))
    cloudwatch.put_metric(NS, "PutRequests", 1, dimensions={"BucketName": bucket})
    cloudwatch.put_metric(NS, "BytesUploaded", len(body), "Bytes", {"BucketName": bucket})
    cloudwatch.put_metric(NS, "PutLatency", (time.time() - start) * 1000, "Milliseconds")
    cloudwatch.log("/aws/s3/" + bucket, f"PUT s3://{bucket}/{key} ({len(body)} bytes, v={meta['VersionId']})")

    from src.aws_cloud import aws_adapter
    aws_adapter.mirror_put(bucket, key, body, meta["ContentType"])

    if fire_events:
        _dispatch_event(bucket, key, "s3:ObjectCreated:Put", meta)
    return meta


def put_file(bucket, key, local_path, **kw):
    return put_object(bucket, key, Path(local_path).read_bytes(), **kw)


def get_object(bucket, key):
    key = _safe_key(key)
    path = _bucket_dir(bucket) / key
    if not key or not path.is_file():
        cloudwatch.put_metric(NS, "4xxErrors", 1, dimensions={"BucketName": bucket})
        raise NoSuchKey(f"s3://{bucket}/{key}")
    cloudwatch.put_metric(NS, "GetRequests", 1, dimensions={"BucketName": bucket})
    return path.read_bytes()


def local_path(bucket, key):
    """Path of an object on the emulated storage volume (used by Glue/SageMaker workers)."""
    path = _bucket_dir(bucket) / _safe_key(key)
    if not path.exists():
        raise NoSuchKey(f"s3://{bucket}/{key}")
    return path


def head_object(bucket, key):
    mp = _meta_path(bucket, _safe_key(key))
    if not mp.exists():
        raise NoSuchKey(f"s3://{bucket}/{key}")
    return json.loads(mp.read_text())


def exists(bucket, key):
    return (_bucket_dir(bucket) / _safe_key(key)).exists()


def delete_object(bucket, key):
    key = _safe_key(key)
    with _LOCK:
        for p in (_bucket_dir(bucket) / key, _meta_path(bucket, key)):
            if p.exists():
                p.unlink()
    cloudwatch.put_metric(NS, "DeleteRequests", 1, dimensions={"BucketName": bucket})
    cloudwatch.log("/aws/s3/" + bucket, f"DELETE s3://{bucket}/{key}")
    _dispatch_event(bucket, key, "s3:ObjectRemoved:Delete", {"Key": key})


def list_objects(bucket, prefix=""):
    root = _bucket_dir(bucket)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            key = p.relative_to(root).as_posix()
            if key.startswith(prefix):
                mp = _meta_path(bucket, key)
                meta = json.loads(mp.read_text()) if mp.exists() else {
                    "Key": key, "Size": p.stat().st_size, "LastModified": cloudwatch.now_iso(p.stat().st_mtime)}
                out.append(meta)
    return out


def list_versions(bucket, key):
    vdir = config.S3_ROOT / ".versions" / bucket / _safe_key(key)
    if not vdir.exists():
        return []
    return [{"VersionId": v.name, "Size": v.stat().st_size,
             "LastModified": cloudwatch.now_iso(v.stat().st_mtime)} for v in sorted(vdir.iterdir())]


def presigned_url(bucket, key, expires_in=3600):
    exp = int(time.time()) + expires_in
    sig = _sign(bucket, key, exp)
    return f"/api/v1/s3/download?bucket={bucket}&key={quote(key)}&expires={exp}&signature={sig}"


def verify_presigned(bucket, key, expires, signature):
    try:
        if int(expires) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(_sign(bucket, key, expires), str(signature))


def _sign(bucket, key, expires):
    """HMAC-SHA256 signature (like AWS SigV4 query-string signing)."""
    msg = f"GET\n{bucket}\n{key}\n{expires}".encode()
    return hmac.new(config.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


# ------------------------------------------------------- Notifications --------
def put_bucket_notification(bucket, handler, prefix="", suffix="", events=("s3:ObjectCreated:*",), name=None):
    _NOTIFICATIONS.setdefault(bucket, []).append(
        {"prefix": prefix, "suffix": suffix, "events": events, "handler": handler,
         "name": name or getattr(handler, "__name__", "handler")})


def notification_config(bucket):
    return [{"Id": n["name"], "Events": list(n["events"]),
             "Filter": {"Prefix": n["prefix"], "Suffix": n["suffix"]}} for n in _NOTIFICATIONS.get(bucket, [])]


def _dispatch_event(bucket, key, event_name, meta):
    for n in _NOTIFICATIONS.get(bucket, []):
        if not key.startswith(n["prefix"]) or not key.endswith(n["suffix"]):
            continue
        if not any(event_name.startswith(e.rstrip("*")) for e in n["events"]):
            continue
        event = {"Records": [{
            "eventSource": "aws:s3", "awsRegion": config.AWS_REGION, "eventName": event_name,
            "eventTime": cloudwatch.now_iso(),
            "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": meta.get("Size", 0),
                                                           "eTag": meta.get("ETag")}},
        }]}
        n["handler"](event)
