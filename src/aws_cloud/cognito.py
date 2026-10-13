"""
Amazon Cognito User Pool emulation - sign-up, sign-in, groups and JWT-style tokens.

Passwords are stored as salted PBKDF2 hashes (werkzeug). Sign-in returns a signed,
time-limited ID token (itsdangerous) whose claims mirror Cognito's
(``sub``, ``email``, ``cognito:groups``). The API Gateway authorizer verifies it.
"""
import re
import sqlite3
import threading
import time
import uuid

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from src import config
from src.aws_cloud import cloudwatch

_LOCK = threading.RLock()
_DB = config.CLOUD_ROOT / "cognito" / "user_pool.db"
USER_POOL_ID = f"{config.AWS_REGION}_UHIgeoAI1"
CLIENT_ID = "uhi-dashboard-web-client"
GROUPS = ["admin", "analyst", "viewer"]
_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="cognito-id-token")

PASSWORD_POLICY = "Min 8 chars with upper-case, lower-case, number and symbol"


class AuthError(Exception):
    pass


def _conn():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _LOCK, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            sub TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, password_hash TEXT,
            grp TEXT, status TEXT, created REAL, last_login REAL, login_count INTEGER DEFAULT 0)""")
    if not get_user(config.ADMIN_EMAIL):
        sign_up(config.ADMIN_EMAIL, config.ADMIN_PASSWORD, "Rohit Kumar (Admin)", group="admin")


def _check_password(pw):
    if len(pw) < 8 or not re.search(r"[A-Z]", pw) or not re.search(r"[a-z]", pw) \
            or not re.search(r"\d", pw) or not re.search(r"[^A-Za-z0-9]", pw):
        raise AuthError("Password does not meet policy: " + PASSWORD_POLICY)


def sign_up(email, password, name, group="viewer"):
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise AuthError("Enter a valid email address")
    _check_password(password)
    if group not in GROUPS:
        group = "viewer"
    if get_user(email):
        raise AuthError("An account with this email already exists")
    sub = str(uuid.uuid4())
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO users (sub,email,name,password_hash,grp,status,created) VALUES (?,?,?,?,?,?,?)",
                  (sub, email, name or email.split("@")[0], generate_password_hash(password), group, "CONFIRMED", time.time()))
    cloudwatch.log("/aws/cognito/userpool/" + USER_POOL_ID, f"SignUp {email} group={group}")
    cloudwatch.put_metric("AWS/Cognito", "SignUpSuccesses", 1)
    return sub


def get_user(email):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE email=?", ((email or "").lower(),)).fetchone()
    return dict(r) if r else None


def initiate_auth(email, password):
    user = get_user(email)
    if not user or not check_password_hash(user["password_hash"], password or ""):
        cloudwatch.put_metric("AWS/Cognito", "SignInFailures", 1)
        cloudwatch.log("/aws/cognito/userpool/" + USER_POOL_ID, f"Failed sign-in for {email}", "WARN")
        raise AuthError("Incorrect email or password")
    if user["status"] != "CONFIRMED":
        raise AuthError("User is disabled")
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET last_login=?, login_count=login_count+1 WHERE sub=?", (time.time(), user["sub"]))
    claims = {"sub": user["sub"], "email": user["email"], "name": user["name"],
              "cognito:groups": [user["grp"]], "iss": f"https://cognito-idp.{config.AWS_REGION}.amazonaws.com/{USER_POOL_ID}",
              "aud": CLIENT_ID, "token_use": "id", "auth_time": int(time.time())}
    cloudwatch.put_metric("AWS/Cognito", "SignInSuccesses", 1)
    cloudwatch.log("/aws/cognito/userpool/" + USER_POOL_ID, f"Sign-in {email}")
    return {"IdToken": _signer.dumps(claims), "ExpiresIn": config.TOKEN_TTL_SECONDS, "TokenType": "Bearer", "claims": claims}


def verify_token(token):
    try:
        return _signer.loads(token, max_age=config.TOKEN_TTL_SECONDS)
    except SignatureExpired:
        raise AuthError("Token expired")
    except BadSignature:
        raise AuthError("Invalid token")


def list_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created").fetchall()
    return [{"sub": r["sub"], "email": r["email"], "name": r["name"], "group": r["grp"], "status": r["status"],
             "created": cloudwatch.now_iso(r["created"]),
             "lastLogin": cloudwatch.now_iso(r["last_login"]) if r["last_login"] else "-",
             "logins": r["login_count"]} for r in rows]


def admin_update_user(email, group=None, status=None):
    with _LOCK, _conn() as c:
        if group in GROUPS:
            c.execute("UPDATE users SET grp=? WHERE email=?", (group, email))
        if status in ("CONFIRMED", "DISABLED"):
            c.execute("UPDATE users SET status=? WHERE email=?", (status, email))
    cloudwatch.log("/aws/cognito/userpool/" + USER_POOL_ID, f"AdminUpdateUser {email} group={group} status={status}")
