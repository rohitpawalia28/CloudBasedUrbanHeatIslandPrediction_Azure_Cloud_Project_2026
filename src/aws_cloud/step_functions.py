"""
AWS Step Functions emulation - orchestrates the end-to-end UHI workflow.

A state machine is an ordered list of Task / Parallel states. Each state is a Python
callable ``fn(input) -> output`` whose output becomes the next state's input, with
per-state retries (Retry: MaxAttempts) and a Catch that fails the execution.
Execution history is persisted so the console can render the visual workflow.
"""
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from src import config
from src.aws_cloud import cloudwatch
from src.aws_cloud._jobstore import JobStore

_MACHINES = {}
_EXEC = JobStore("stepfunctions", "executions.json")


def _arn(kind, name):
    return f"arn:aws:states:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:{kind}:{name}"


def create_state_machine(name, states, comment=""):
    """states: list of dicts {Name, Type: Task|Parallel, Resource, Fn | Branches:[(name, fn)], Retry}"""
    _MACHINES[name] = {"name": name, "states": states, "comment": comment,
                       "arn": _arn("stateMachine", name), "created": cloudwatch.now_iso()}


def definition(name):
    m = _MACHINES[name]
    out = {"Comment": m["comment"], "StartAt": m["states"][0]["Name"], "States": {}}
    for i, s in enumerate(m["states"]):
        d = {"Type": s["Type"], "Resource": s.get("Resource", "")}
        if s["Type"] == "Parallel":
            d["Branches"] = [b[0] for b in s["Branches"]]
        if s.get("Retry"):
            d["Retry"] = [{"ErrorEquals": ["States.ALL"], "MaxAttempts": s["Retry"], "BackoffRate": 2}]
        if i + 1 < len(m["states"]):
            d["Next"] = m["states"][i + 1]["Name"]
        else:
            d["End"] = True
        out["States"][s["Name"]] = d
    return out


def list_state_machines():
    return [{"name": m["name"], "arn": m["arn"], "comment": m["comment"], "states": len(m["states"]),
             "created": m["created"]} for m in _MACHINES.values()]


def start_execution(name, payload=None, started_by="system", wait=False):
    m = _MACHINES[name]
    exec_name = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    _EXEC.put(exec_name, {
        "executionArn": _arn("execution", f"{name}:{exec_name}"), "name": exec_name, "stateMachine": name,
        "status": "RUNNING", "startDate": cloudwatch.now_iso(), "stopDate": None, "startedBy": started_by,
        "input": payload or {}, "output": None, "error": None,
        "states": [{"name": s["Name"], "type": s["Type"], "resource": s.get("Resource", ""),
                    "status": "PENDING", "start": None, "end": None, "durationS": None, "output": None,
                    "attempts": 0} for s in m["states"]],
    })
    t = threading.Thread(target=_run, args=(m, exec_name, {"input": payload or {}}), daemon=True)
    t.start()
    if wait:
        t.join()
    return exec_name


def _set_state(exec_name, idx, **fields):
    with _EXEC.lock:
        ex = _EXEC.get(exec_name)
        ex["states"][idx].update(fields)
        _EXEC.put(exec_name, ex)


def _run(machine, exec_name, data):
    group = "/aws/vendedlogs/states/" + machine["name"]
    cloudwatch.log(group, f"ExecutionStarted {exec_name}")
    start_all = time.time()
    for idx, st in enumerate(machine["states"]):
        _set_state(exec_name, idx, status="RUNNING", start=cloudwatch.now_iso())
        cloudwatch.log(group, f"[{exec_name}] TaskStateEntered: {st['Name']}")
        t0 = time.time()
        attempts, max_attempts = 0, 1 + int(st.get("Retry", 0))
        while True:
            attempts += 1
            try:
                if st["Type"] == "Parallel":
                    with ThreadPoolExecutor(max_workers=len(st["Branches"])) as pool:
                        futs = {b: pool.submit(fn, data) for b, fn in st["Branches"]}
                        out = {b: f.result() for b, f in futs.items()}
                else:
                    out = st["Fn"](data)
                break
            except Exception as exc:
                cloudwatch.log(group, f"[{exec_name}] {st['Name']} attempt {attempts} failed: {exc}", "ERROR")
                if attempts >= max_attempts:
                    _set_state(exec_name, idx, status="FAILED", end=cloudwatch.now_iso(),
                               durationS=round(time.time() - t0, 2), output={"error": str(exc)}, attempts=attempts)
                    for j in range(idx + 1, len(machine["states"])):
                        _set_state(exec_name, j, status="SKIPPED")
                    _EXEC.update(exec_name, status="FAILED", stopDate=cloudwatch.now_iso(),
                                 error=f"{st['Name']}: {exc.__class__.__name__}: {exc}")
                    cloudwatch.log(group, traceback.format_exc(), "ERROR")
                    cloudwatch.put_metric("AWS/States", "ExecutionsFailed", 1, dimensions={"StateMachineArn": machine["arn"]})
                    from src.aws_cloud import sns
                    sns.publish("uhi-pipeline-alerts", f"Pipeline FAILED at {st['Name']}",
                                f"Execution {exec_name} failed in state {st['Name']}: {exc}")
                    return
                time.sleep(0.5 * attempts)
        data = {**data, st["Name"]: out}
        _set_state(exec_name, idx, status="SUCCEEDED", end=cloudwatch.now_iso(),
                   durationS=round(time.time() - t0, 2), output=out, attempts=attempts)
        cloudwatch.log(group, f"[{exec_name}] TaskStateExited: {st['Name']} ({time.time() - t0:.2f}s)")
    total = round(time.time() - start_all, 2)
    _EXEC.update(exec_name, status="SUCCEEDED", stopDate=cloudwatch.now_iso(),
                 output={k: v for k, v in data.items() if k != "input"}, durationS=total)
    cloudwatch.put_metric("AWS/States", "ExecutionsSucceeded", 1, dimensions={"StateMachineArn": machine["arn"]})
    cloudwatch.put_metric("AWS/States", "ExecutionTime", total * 1000, "Milliseconds")
    cloudwatch.log(group, f"ExecutionSucceeded {exec_name} in {total}s")


def list_executions(limit=30):
    return sorted(_EXEC.all(), key=lambda e: e["startDate"], reverse=True)[:limit]


def describe_execution(exec_name):
    return _EXEC.get(exec_name)


def running():
    return any(e["status"] == "RUNNING" for e in _EXEC.all())


def recover_interrupted():
    """Executions left RUNNING by a previous server process are marked ABORTED (like a stopped execution)."""
    for e in _EXEC.all():
        if e["status"] == "RUNNING":
            _EXEC.update(e["name"], status="ABORTED", stopDate=cloudwatch.now_iso(), error="Server restarted")
