"""
Amazon RDS emulation - the relational metadata store.

In AWS this is an RDS PostgreSQL (PostGIS) instance in a private subnet. Locally it
is a SQLite database file with the same schema, accessed only through
parameterised queries. Automated snapshots imitate RDS backups.
"""
import shutil
import sqlite3
import threading
import time

from src import config
from src.aws_cloud import cloudwatch

_LOCK = threading.RLock()

INSTANCE = {
    "DBInstanceIdentifier": "uhi-metadata-db",
    "Engine": "postgres (emulated: sqlite3)", "EngineVersion": "16.3",
    "DBInstanceClass": "db.t4g.micro", "MultiAZ": False, "StorageEncrypted": True,
    "PubliclyAccessible": False, "BackupRetentionPeriod": 7,
    "Endpoint": f"uhi-metadata-db.cluster.{config.AWS_REGION}.rds.amazonaws.com:5432",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version TEXT, s3_uri TEXT,
    source TEXT, rows INTEGER, columns INTEGER, size_bytes INTEGER,
    study_area TEXT, created_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS grid_predictions (
    grid_id TEXT, obs_date TEXT, latitude REAL, longitude REAL,
    lst_celsius REAL, ndvi REAL, ndbi REAL, lulc_code INTEGER,
    uhi_observed REAL, uhi_predicted REAL, risk_level TEXT, model_version TEXT,
    PRIMARY KEY (grid_id, obs_date)
);
CREATE INDEX IF NOT EXISTS ix_grid_date ON grid_predictions(obs_date);
CREATE TABLE IF NOT EXISTS prediction_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_email TEXT, source TEXT,
    latitude REAL, longitude REAL, inputs TEXT, uhi_predicted REAL, risk_level TEXT,
    model_version TEXT, latency_ms REAL
);
CREATE TABLE IF NOT EXISTS hotspot_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, obs_date TEXT, zone TEXT, cells INTEGER,
    mean_uhi REAL, max_uhi REAL, mean_ndvi REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT
);
"""


def _conn():
    config.RDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.RDS_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with _LOCK, _conn() as c:
        c.executescript(SCHEMA)
    cloudwatch.log("/aws/rds/instance/uhi-metadata-db/postgresql", "Database available, schema verified")


def execute(sql, params=()):
    start = time.time()
    with _LOCK, _conn() as c:
        cur = c.execute(sql, params)
        rid = cur.lastrowid
    cloudwatch.put_metric("AWS/RDS", "WriteIOPS", 1)
    cloudwatch.put_metric("AWS/RDS", "WriteLatency", (time.time() - start) * 1000, "Milliseconds")
    return rid


def executemany(sql, rows):
    with _LOCK, _conn() as c:
        c.executemany(sql, rows)
    cloudwatch.put_metric("AWS/RDS", "WriteIOPS", len(rows))


def query(sql, params=()):
    start = time.time()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    cloudwatch.put_metric("AWS/RDS", "ReadIOPS", 1)
    cloudwatch.put_metric("AWS/RDS", "ReadLatency", (time.time() - start) * 1000, "Milliseconds")
    return rows


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def read_only_query(sql, limit=200):
    """Query-editor endpoint: only a single SELECT statement is allowed."""
    s = sql.strip().rstrip(";")
    if not s.lower().startswith(("select", "with")) or ";" in s:
        raise ValueError("Only a single read-only SELECT statement is allowed in the query editor.")
    with _conn() as c:
        c.execute("PRAGMA query_only = ON")
        cur = c.execute(s)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(limit)]
    return {"columns": cols, "rows": rows}


def tables():
    rows = query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [{"table": r["name"], "rows": query_one(f'SELECT COUNT(*) n FROM "{r["name"]}"')["n"]} for r in rows]


def audit(actor, action, detail=""):
    execute("INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
            (cloudwatch.now_iso(), actor, action, detail))


def snapshot():
    snap_dir = config.CLOUD_ROOT / "rds" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    name = f"uhi-metadata-db-snap-{time.strftime('%Y%m%d-%H%M%S')}"
    with _LOCK:
        shutil.copy2(config.RDS_PATH, snap_dir / (name + ".db"))
    cloudwatch.log("/aws/rds/instance/uhi-metadata-db/postgresql", f"Manual snapshot {name} created")
    return name


def snapshots():
    snap_dir = config.CLOUD_ROOT / "rds" / "snapshots"
    if not snap_dir.exists():
        return []
    return [{"DBSnapshotIdentifier": p.stem, "Size": p.stat().st_size,
             "SnapshotCreateTime": cloudwatch.now_iso(p.stat().st_mtime)} for p in sorted(snap_dir.glob("*.db"), reverse=True)]
