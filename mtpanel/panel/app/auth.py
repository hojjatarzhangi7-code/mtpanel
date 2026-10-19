import base64
import hashlib
import hmac
import secrets
import time

from . import db
from .config import SECRET_KEY, SESSION_TTL


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, h_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h, base64.b64decode(h_b64))
    except Exception:
        return False


def _sig(payload: str, pw_hash: str) -> str:
    # Binding the signature to the password hash invalidates sessions on password change.
    key = SECRET_KEY + pw_hash.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(username: str, pw_hash: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{username}|{exp}"
    raw = f"{payload}|{_sig(payload, pw_hash)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def check_session(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit("|", 2)
        if int(exp) < time.time():
            return None
        admin = db.q1("SELECT * FROM admins WHERE username=?", (username,))
        if not admin:
            return None
        if not hmac.compare_digest(sig, _sig(f"{username}|{exp}", admin["pw_hash"])):
            return None
        return admin
    except Exception:
        return None


# --- naive in-memory login throttling ---
_attempts = {}


def login_allowed(ip: str) -> bool:
    cnt, ts = _attempts.get(ip, (0, 0))
    if time.time() - ts > 900:
        return True
    return cnt < 8


def login_failed(ip: str):
    cnt, ts = _attempts.get(ip, (0, time.time()))
    if time.time() - ts > 900:
        cnt, ts = 0, time.time()
    _attempts[ip] = (cnt + 1, ts)


def login_ok(ip: str):
    _attempts.pop(ip, None)


def ensure_admin(username: str, password: str, force: bool = False):
    """Create the first admin. With force (fresh install), the given credentials always win,
    so re-installing over an old database still logs in with what the installer printed."""
    if not username or not password:
        return
    if force:
        first = db.q1("SELECT id FROM admins ORDER BY id LIMIT 1")
        if first:
            with db.tx() as c:
                c.execute("DELETE FROM admins WHERE username=? AND id<>?", (username, first["id"]))
                c.execute("UPDATE admins SET username=?, pw_hash=? WHERE id=?",
                          (username, hash_password(password), first["id"]))
            return
    if db.q1("SELECT id FROM admins WHERE username=?", (username,)):
        return
    if db.q1("SELECT id FROM admins"):
        return  # some admin already exists; don't silently add another
    with db.tx() as c:
        c.execute(
            "INSERT INTO admins(username,pw_hash,created_at) VALUES(?,?,?)",
            (username, hash_password(password), db.now()),
        )
