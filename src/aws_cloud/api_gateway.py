"""
Amazon API Gateway emulation - authorizer, IAM authorization, throttling, API keys,
usage plans, access logging and request metrics for every /api/v1 route.

Clients authenticate with either
  * ``Authorization: Bearer <Cognito IdToken>`` (dashboard users), or
  * ``x-api-key: <key>`` (machine clients, bound to a usage plan).
"""
import functools
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque

from flask import g, jsonify, request, session

from src import config
from src.aws_cloud import cloudwatch, cognito, iam

API_ID = "uhi7geo2ai"
STAGE = "prod"
INVOKE_URL = f"https://{API_ID}.execute-api.{config.AWS_REGION}.amazonaws.com/{STAGE}"
_DB = config.CLOUD_ROOT / "apigateway" / "api_keys.db"
_LOCK = threading.RLock()
_WINDOWS = defaultdict(deque)
ROUTES = []


def _conn():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _LOCK, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, name TEXT, value TEXT UNIQUE,
                     owner TEXT, grp TEXT, enabled INTEGER, created REAL, requests INTEGER DEFAULT 0)""")


def create_api_key(name, owner, group):
    kid = secrets.token_hex(5)
    value = "uhi_" + secrets.token_urlsafe(28)
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,1,?,0)", (kid, name, value, owner, group, time.time()))
    cloudwatch.log("/aws/apigateway/" + API_ID, f"API key {name} ({kid}) created for {owner}")
    return {"id": kid, "name": name, "value": value}


def list_api_keys(owner=None):
    with _conn() as c:
        rows = c.execute("SELECT * FROM api_keys" + (" WHERE owner=?" if owner else "") + " ORDER BY created DESC",
                         (owner,) if owner else ()).fetchall()
    return [{"id": r["id"], "name": r["name"], "valuePreview": r["value"][:8] + "..." + r["value"][-4:],
             "owner": r["owner"], "group": r["grp"], "enabled": bool(r["enabled"]),
             "created": cloudwatch.now_iso(r["created"]), "requests": r["requests"]} for r in rows]


def delete_api_key(kid, owner=None):
    with _LOCK, _conn() as c:
        if owner:
            c.execute("DELETE FROM api_keys WHERE id=? AND owner=?", (kid, owner))
        else:
            c.execute("DELETE FROM api_keys WHERE id=?", (kid,))


def _resolve_api_key(value):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM api_keys WHERE value=? AND enabled=1", (value,)).fetchone()
        if r:
            c.execute("UPDATE api_keys SET requests=requests+1 WHERE id=?", (r["id"],))
    return dict(r) if r else None


def _throttled(identity):
    now = time.time()
    with _LOCK:
        win = _WINDOWS[identity]
        while win and win[0] < now - 60:
            win.popleft()
        if len(win) >= config.API_RATE_LIMIT_PER_MIN:
            return True
        win.append(now)
    return False


def _error(status, message, start):
    _record(status, start)
    return jsonify({"message": message, "status": status}), status


def _record(status, start):
    latency = (time.time() - start) * 1000
    cloudwatch.put_metric("AWS/ApiGateway", "Count", 1, dimensions={"ApiName": "uhi-api", "Stage": STAGE})
    cloudwatch.put_metric("AWS/ApiGateway", "Latency", latency, "Milliseconds", {"ApiName": "uhi-api"})
    if 400 <= status < 500:
        cloudwatch.put_metric("AWS/ApiGateway", "4XXError", 1, dimensions={"ApiName": "uhi-api"})
    elif status >= 500:
        cloudwatch.put_metric("AWS/ApiGateway", "5XXError", 1, dimensions={"ApiName": "uhi-api"})
    who = getattr(g, "identity", {}).get("email", "-") if hasattr(g, "identity") else "-"
    cloudwatch.log(f"/aws/apigateway/{API_ID}/{STAGE}/access",
                   f'{request.remote_addr} {who} "{request.method} {request.path}" {status} {latency:.1f}ms')


def endpoint(action, auth=True, description=""):
    """Decorator applied to every API route: authorizer + IAM + throttling + logging."""
    def deco(fn):
        ROUTES.append({"function": fn.__name__, "action": action, "auth": auth, "description": description})

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            g.identity = {"email": "anonymous", "group": "public"}
            if auth:
                token = request.headers.get("Authorization", "").replace("Bearer ", "").strip() or session.get("id_token")
                key = request.headers.get("x-api-key")
                if key:
                    k = _resolve_api_key(key)
                    if not k:
                        return _error(403, "Forbidden: invalid API key", start)
                    g.identity = {"email": k["owner"], "group": k["grp"], "via": "api-key"}
                elif token:
                    try:
                        claims = cognito.verify_token(token)
                    except cognito.AuthError as exc:
                        return _error(401, f"Unauthorized: {exc}", start)
                    g.identity = {"email": claims["email"], "name": claims.get("name"),
                                  "group": claims["cognito:groups"][0], "via": "cognito"}
                else:
                    return _error(401, "Unauthorized: missing Cognito token or x-api-key", start)
                if not iam.group_allowed(g.identity["group"], action):
                    return _error(403, f"AccessDenied: group '{g.identity['group']}' is not allowed to perform {action}", start)
            ident = g.identity.get("email") + request.remote_addr
            if _throttled(ident):
                return _error(429, "Too Many Requests: usage plan rate limit exceeded", start)
            try:
                resp = fn(*args, **kwargs)
            except (ValueError, KeyError) as exc:
                return _error(400, f"Bad request: {exc}", start)
            except RuntimeError as exc:
                return _error(409, str(exc), start)
            except Exception as exc:  # noqa
                cloudwatch.log(f"/aws/apigateway/{API_ID}/{STAGE}/execution", f"{fn.__name__}: {exc!r}", "ERROR")
                return _error(500, f"Internal server error: {exc}", start)
            status = resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)
            _record(status, start)
            return resp
        return wrapper
    return deco
