"""
Amazon CloudWatch emulation: log groups / streams, custom metrics and alarms.

Every other emulated service writes its logs and metrics here, so the
"Monitoring" console page shows a real, live view of the platform.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

from src import config
from src.aws_cloud import aws_adapter

_LOCK = threading.RLock()
_DB = config.CLOUDWATCH_ROOT / "cloudwatch.db"


def _conn():
    config.CLOUDWATCH_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _LOCK, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS log_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, log_group TEXT, log_stream TEXT, level TEXT, message TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_log_group ON log_events(log_group, ts);
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, namespace TEXT, name TEXT, value REAL, unit TEXT, dimensions TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_metric ON metrics(namespace, name, ts);
            CREATE TABLE IF NOT EXISTS alarms (
                name TEXT PRIMARY KEY, namespace TEXT, metric TEXT, comparison TEXT,
                threshold REAL, period_s INTEGER, statistic TEXT, sns_topic TEXT,
                state TEXT DEFAULT 'OK', state_reason TEXT, updated REAL
            );
            """
        )


def now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- Logs --------
def log(group: str, message: str, level: str = "INFO", stream: str = None):
    stream = stream or datetime.now().strftime("%Y/%m/%d")
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO log_events (ts, log_group, log_stream, level, message) VALUES (?,?,?,?,?)",
            (time.time(), group, stream, level.upper(), str(message)[:4000]),
        )
    aws_adapter.mirror_log(group, str(message)[:4000], level.upper())


def log_groups():
    with _conn() as c:
        rows = c.execute(
            "SELECT log_group, COUNT(*) n, MAX(ts) last FROM log_events GROUP BY log_group ORDER BY log_group"
        ).fetchall()
    return [{"logGroupName": r["log_group"], "events": r["n"], "lastEvent": now_iso(r["last"])} for r in rows]


def filter_logs(group=None, level=None, query=None, limit=200):
    sql, args = "SELECT * FROM log_events WHERE 1=1", []
    if group:
        sql += " AND log_group = ?"; args.append(group)
    if level:
        sql += " AND level = ?"; args.append(level.upper())
    if query:
        sql += " AND message LIKE ?"; args.append(f"%{query}%")
    sql += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        {"timestamp": now_iso(r["ts"]), "logGroup": r["log_group"], "logStream": r["log_stream"],
         "level": r["level"], "message": r["message"]}
        for r in rows
    ]


# ------------------------------------------------------------- Metrics --------
def put_metric(namespace: str, name: str, value: float, unit: str = "Count", dimensions: dict = None):
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO metrics (ts, namespace, name, value, unit, dimensions) VALUES (?,?,?,?,?,?)",
            (time.time(), namespace, name, float(value), unit, json.dumps(dimensions or {})),
        )
    aws_adapter.mirror_metric(namespace, name, value, unit, dimensions)
    _evaluate_alarms(namespace, name)


def metric_statistics(namespace, name, period_s=60, minutes=60, statistic="Sum"):
    since = time.time() - minutes * 60
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, value FROM metrics WHERE namespace=? AND name=? AND ts>=? ORDER BY ts",
            (namespace, name, since),
        ).fetchall()
    buckets = {}
    for r in rows:
        b = int(r["ts"] // period_s) * period_s
        buckets.setdefault(b, []).append(r["value"])
    out = []
    for b in sorted(buckets):
        vals = buckets[b]
        agg = {"Sum": sum(vals), "Average": sum(vals) / len(vals), "Maximum": max(vals),
               "Minimum": min(vals), "SampleCount": len(vals)}[statistic]
        out.append({"timestamp": now_iso(b), "value": round(agg, 4)})
    return out


def list_metrics():
    with _conn() as c:
        rows = c.execute(
            "SELECT namespace, name, unit, COUNT(*) n, MAX(ts) last FROM metrics GROUP BY namespace, name ORDER BY namespace, name"
        ).fetchall()
    return [{"namespace": r["namespace"], "metricName": r["name"], "unit": r["unit"],
             "datapoints": r["n"], "last": now_iso(r["last"])} for r in rows]


def metric_total(namespace, name, minutes=None):
    sql, args = "SELECT COALESCE(SUM(value),0) s FROM metrics WHERE namespace=? AND name=?", [namespace, name]
    if minutes:
        sql += " AND ts>=?"; args.append(time.time() - minutes * 60)
    with _conn() as c:
        return c.execute(sql, args).fetchone()["s"]


# -------------------------------------------------------------- Alarms --------
def put_alarm(name, namespace, metric, comparison, threshold, period_s=300, statistic="Maximum", sns_topic=None):
    with _LOCK, _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO alarms (name, namespace, metric, comparison, threshold, period_s,
               statistic, sns_topic, state, state_reason, updated)
               VALUES (?,?,?,?,?,?,?,?, COALESCE((SELECT state FROM alarms WHERE name=?),'OK'),
               'Alarm created', ?)""",
            (name, namespace, metric, comparison, float(threshold), int(period_s), statistic, sns_topic, name, time.time()),
        )


def list_alarms():
    with _conn() as c:
        rows = c.execute("SELECT * FROM alarms ORDER BY name").fetchall()
    return [dict(r) | {"updated": now_iso(r["updated"])} for r in rows]


def delete_alarm(name):
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM alarms WHERE name=?", (name,))


def _evaluate_alarms(namespace, name):
    from src.aws_cloud import sns  # local import to avoid a cycle

    with _conn() as c:
        alarms = c.execute("SELECT * FROM alarms WHERE namespace=? AND metric=?", (namespace, name)).fetchall()
    for a in alarms:
        stats = metric_statistics(namespace, name, period_s=a["period_s"], minutes=max(1, a["period_s"] // 60), statistic=a["statistic"])
        if not stats:
            continue
        value = stats[-1]["value"]
        breach = {
            "GreaterThanThreshold": value > a["threshold"],
            "GreaterThanOrEqualToThreshold": value >= a["threshold"],
            "LessThanThreshold": value < a["threshold"],
        }.get(a["comparison"], False)
        new_state = "ALARM" if breach else "OK"
        if new_state != a["state"]:
            reason = f"{a['statistic']} of {name} = {value} ({a['comparison']} {a['threshold']})"
            with _LOCK, _conn() as c:
                c.execute("UPDATE alarms SET state=?, state_reason=?, updated=? WHERE name=?",
                          (new_state, reason, time.time(), a["name"]))
            log("/aws/cloudwatch/alarms", f"Alarm '{a['name']}' changed to {new_state}: {reason}",
                "WARN" if breach else "INFO")
            if breach and a["sns_topic"]:
                sns.publish(a["sns_topic"], f"CloudWatch ALARM: {a['name']}",
                            f"Alarm {a['name']} entered ALARM state. {reason}")
