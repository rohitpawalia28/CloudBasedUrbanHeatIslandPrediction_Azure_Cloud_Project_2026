"""
Amazon SNS emulation - topics, subscriptions and message delivery.

Protocols: ``email`` and ``sms`` (delivery is recorded in the outbox - nothing is
actually sent from a laptop), ``https`` (real HTTP POST to a webhook) and ``app``
(in-console notification feed). In CLOUD_MODE=aws, messages are also published to
the real SNS topic through boto3.
"""
import json
import sqlite3
import threading
import time
import uuid

from src import config
from src.aws_cloud import cloudwatch

_LOCK = threading.RLock()
_DB = config.CLOUD_ROOT / "sns" / "sns.db"


def _conn():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def arn(topic):
    return f"arn:aws:sns:{config.AWS_REGION}:{config.AWS_ACCOUNT_ID}:{topic}"


def init():
    with _LOCK, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (name TEXT PRIMARY KEY, display_name TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY, topic TEXT, protocol TEXT, endpoint TEXT,
                status TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, topic TEXT, subject TEXT, body TEXT,
                attributes TEXT, ts REAL, deliveries TEXT, read INTEGER DEFAULT 0);
            """
        )


def create_topic(name, display_name=None):
    with _LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO topics VALUES (?,?,?)", (name, display_name or name, time.time()))
    return arn(name)


def list_topics():
    with _conn() as c:
        rows = c.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM subscriptions s WHERE s.topic=t.name) subs,
                      (SELECT COUNT(*) FROM messages m WHERE m.topic=t.name) msgs
               FROM topics t ORDER BY name""").fetchall()
    return [{"TopicArn": arn(r["name"]), "Name": r["name"], "DisplayName": r["display_name"],
             "Subscriptions": r["subs"], "Messages": r["msgs"]} for r in rows]


def subscribe(topic, protocol, endpoint):
    sid = uuid.uuid4().hex[:12]
    with _LOCK, _conn() as c:
        exists = c.execute("SELECT id FROM subscriptions WHERE topic=? AND protocol=? AND endpoint=?",
                           (topic, protocol, endpoint)).fetchone()
        if exists:
            return exists["id"]
        c.execute("INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
                  (sid, topic, protocol, endpoint, "Confirmed", time.time()))
    cloudwatch.log("/aws/sns/" + topic, f"New {protocol} subscription: {endpoint}")
    return sid


def unsubscribe(sid):
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))


def list_subscriptions(topic=None):
    with _conn() as c:
        if topic:
            rows = c.execute("SELECT * FROM subscriptions WHERE topic=?", (topic,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM subscriptions ORDER BY topic").fetchall()
    return [{"SubscriptionArn": f"{arn(r['topic'])}:{r['id']}", "Id": r["id"], "Topic": r["topic"],
             "Protocol": r["protocol"], "Endpoint": r["endpoint"], "Status": r["status"]} for r in rows]


def publish(topic, subject, message, attributes=None):
    create_topic(topic)
    mid = str(uuid.uuid4())
    deliveries = []
    for s in list_subscriptions(topic):
        status = "DELIVERED"
        if s["Protocol"] == "https":
            try:
                import urllib.request
                req = urllib.request.Request(s["Endpoint"], data=json.dumps(
                    {"Type": "Notification", "MessageId": mid, "Subject": subject, "Message": message}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=4)
            except Exception as exc:  # webhook unreachable
                status = f"FAILED ({exc.__class__.__name__})"
        deliveries.append({"protocol": s["Protocol"], "endpoint": s["Endpoint"], "status": status})
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,0)",
                  (mid, topic, subject, message, json.dumps(attributes or {}), time.time(), json.dumps(deliveries)))
    cloudwatch.put_metric("AWS/SNS", "NumberOfMessagesPublished", 1, dimensions={"TopicName": topic})
    cloudwatch.put_metric("AWS/SNS", "NumberOfNotificationsDelivered",
                          sum(1 for d in deliveries if d["status"] == "DELIVERED"))
    cloudwatch.log("/aws/sns/" + topic, f"Published {mid}: {subject} -> {len(deliveries)} subscriber(s)")

    from src.aws_cloud import aws_adapter
    aws_adapter.mirror_publish(topic, subject, message)
    return mid


def messages(topic=None, limit=50):
    with _conn() as c:
        if topic:
            rows = c.execute("SELECT * FROM messages WHERE topic=? ORDER BY ts DESC LIMIT ?", (topic, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [{"MessageId": r["id"], "Topic": r["topic"], "Subject": r["subject"], "Message": r["body"],
             "Attributes": json.loads(r["attributes"]), "Timestamp": cloudwatch.now_iso(r["ts"]),
             "Deliveries": json.loads(r["deliveries"]), "Read": bool(r["read"])} for r in rows]


def unread_count():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM messages WHERE read=0").fetchone()["n"]


def mark_all_read():
    with _LOCK, _conn() as c:
        c.execute("UPDATE messages SET read=1")
