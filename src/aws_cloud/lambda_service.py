"""
AWS Lambda emulation - serverless functions with event triggers.

Functions are plain Python handlers ``handler(event, context)``. Invocations can be
synchronous (RequestResponse) or asynchronous (Event, run on a worker thread).
Every invocation emits REPORT logs and Invocations / Errors / Duration metrics.
"""
import threading
import time
import traceback
import uuid
from collections import deque

from src import config
from src.aws_cloud import cloudwatch

_FUNCTIONS = {}
_INVOCATIONS = deque(maxlen=200)
_LOCK = threading.RLock()


class Context:
    def __init__(self, fn):
        self.function_name = fn["FunctionName"]
        self.memory_limit_in_mb = fn["MemorySize"]
        self.aws_request_id = str(uuid.uuid4())
        self.invoked_function_arn = fn["FunctionArn"]
        self.log_group_name = "/aws/lambda/" + fn["FunctionName"]
        self._deadline = time.time() + fn["Timeout"]

    def get_remaining_time_in_millis(self):
        return max(0, int((self._deadline - time.time()) * 1000))

    def log(self, msg, level="INFO"):
        cloudwatch.log(self.log_group_name, f"[{self.aws_request_id[:8]}] {msg}", level)


def create_function(name, handler, description="", memory=512, timeout=120, role="UHI-LambdaExecutionRole",
                    trigger=None):
    _FUNCTIONS[name] = {
        "FunctionName": name, "_handler": handler, "Description": description,
        "FunctionArn": f"arn:aws:lambda:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:function:{name}",
        "Runtime": "python3.12", "Handler": f"{handler.__module__}.{handler.__name__}",
        "MemorySize": memory, "Timeout": timeout, "Role": role, "Trigger": trigger or "manual",
        "Invocations": 0, "Errors": 0, "LastModified": cloudwatch.now_iso(),
    }


def list_functions():
    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in _FUNCTIONS.values()]


def invocations(limit=50):
    return list(_INVOCATIONS)[-limit:][::-1]


def invoke(name, event, invocation_type="RequestResponse"):
    fn = _FUNCTIONS.get(name)
    if not fn:
        raise KeyError(f"Function not found: {name}")
    if invocation_type == "Event":
        threading.Thread(target=_run, args=(fn, event), daemon=True).start()
        return {"StatusCode": 202}
    return _run(fn, event)


def _run(fn, event):
    ctx = Context(fn)
    start = time.time()
    ctx.log(f"START RequestId: {ctx.aws_request_id} Version: $LATEST")
    status, result, error = 200, None, None
    try:
        result = fn["_handler"](event, ctx)
    except Exception as exc:
        status, error = 500, f"{exc.__class__.__name__}: {exc}"
        ctx.log(traceback.format_exc(), "ERROR")
    duration = (time.time() - start) * 1000
    billed = max(1, int(-(-duration // 1)))
    ctx.log(f"END RequestId: {ctx.aws_request_id}")
    ctx.log(f"REPORT RequestId: {ctx.aws_request_id} Duration: {duration:.2f} ms Billed Duration: {billed} ms "
            f"Memory Size: {fn['MemorySize']} MB")
    with _LOCK:
        fn["Invocations"] += 1
        if error:
            fn["Errors"] += 1
        _INVOCATIONS.append({"requestId": ctx.aws_request_id, "function": fn["FunctionName"], "status": status,
                             "durationMs": round(duration, 2), "timestamp": cloudwatch.now_iso(), "error": error})
    dims = {"FunctionName": fn["FunctionName"]}
    cloudwatch.put_metric("AWS/Lambda", "Invocations", 1, dimensions=dims)
    cloudwatch.put_metric("AWS/Lambda", "Duration", duration, "Milliseconds", dims)
    if error:
        cloudwatch.put_metric("AWS/Lambda", "Errors", 1, dimensions=dims)
    return {"StatusCode": status, "Payload": result, "FunctionError": error, "RequestId": ctx.aws_request_id}
