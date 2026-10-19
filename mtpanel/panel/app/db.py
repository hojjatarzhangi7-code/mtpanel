import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    agent_port INTEGER NOT NULL DEFAULT 7443,
    token TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    public_host TEXT NOT NULL,
    public_port INTEGER NOT NULL DEFAULT 443,
    tls_domain TEXT NOT NULL,
    ad_tag TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_seen INTEGER,
    last_error TEXT,
    status_json TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    secret TEXT NOT NULL,
    note TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    quota_bytes INTEGER NOT NULL DEFAULT 0,
    used_bytes INTEGER NOT NULL DEFAULT 0,
    expire_at INTEGER,
    max_ips INTEGER,
    max_conns INTEGER,
    rate_up_bps INTEGER,
    rate_down_bps INTEGER,
    ad_tag TEXT,
    node_ids TEXT,
    sub_token TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL,
    online_at INTEGER
);
CREATE TABLE IF NOT EXISTS node_users (
    node_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    last_octets INTEGER NOT NULL DEFAULT 0,
    pushed_hash TEXT,
    connections INTEGER NOT NULL DEFAULT 0,
    active_ips INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    PRIMARY KEY (node_id, username)
);
CREATE TABLE IF NOT EXISTS ad_tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ad_campaigns (
    id INTEGER PRIMARY KEY,
    tag_id INTEGER NOT NULL,
    node_ids TEXT,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    note TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL DEFAULT 0
);
"""


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


MIGRATIONS = [
    ("nodes", "domain", "ALTER TABLE nodes ADD COLUMN domain TEXT"),
    ("nodes", "ip_changed_at", "ALTER TABLE nodes ADD COLUMN ip_changed_at INTEGER"),
]


def init():
    with _write_lock:
        c = conn()
        c.executescript(SCHEMA)
        for table, col, sql in MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                c.execute(sql)


@contextmanager
def tx():
    """Serialized write transaction."""
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    with tx() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def now():
    return int(time.time())


def loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default
