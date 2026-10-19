"""Background reconciler: pushes panel state to every node and pulls usage back."""
import asyncio
import json
import logging
import socket
import threading

from . import db, logic
from .config import SYNC_INTERVAL
from .node_client import NodeClient, NodeError

log = logging.getLogger("mtpanel.sync")

_wake = None          # asyncio.Event, created inside the loop
_loop = None          # the event loop that owns _wake
_node_locks = {}      # node_id -> threading.Lock
_locks_guard = threading.Lock()
RESERVED_PREFIX = "__"


def _lock_for(node_id):
    with _locks_guard:
        return _node_locks.setdefault(node_id, threading.Lock())


def request_sync():
    """Ask the loop to run a sync pass soon (call from request handlers)."""
    if _wake is not None and _loop is not None:
        try:
            _loop.call_soon_threadsafe(_wake.set)
        except RuntimeError:  # loop closed during shutdown
            pass


def _collect_usage(node, remote_users):
    """Accumulate traffic deltas from telemt counters into users.used_bytes."""
    nid = node["id"]
    total_delta = 0
    known = {u["username"]: u for u in db.q("SELECT username FROM users")}
    prev = {r["username"]: r for r in db.q("SELECT * FROM node_users WHERE node_id=?", (nid,))}
    now = db.now()
    with db.tx() as c:
        for ru in remote_users:
            name = ru["username"]
            if name.startswith(RESERVED_PREFIX) or name not in known:
                continue
            cur = int(ru.get("total_octets") or 0)
            p = prev.get(name)
            last = p["last_octets"] if p else 0
            # Counter reset (telemt restart / user re-created) -> count from zero.
            delta = cur - last if cur >= last else cur
            if p is None:
                delta = cur if cur > 0 else 0
            conns = int(ru.get("current_connections") or 0)
            ips = int(ru.get("active_unique_ips") or 0)
            c.execute(
                """INSERT INTO node_users(node_id,username,last_octets,connections,active_ips,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(node_id,username) DO UPDATE SET last_octets=excluded.last_octets,
                   connections=excluded.connections, active_ips=excluded.active_ips, updated_at=excluded.updated_at""",
                (nid, name, cur, conns, ips, now),
            )
            if delta > 0:
                c.execute("UPDATE users SET used_bytes=used_bytes+? WHERE username=?", (delta, name))
                total_delta += delta
            if conns > 0:
                c.execute("UPDATE users SET online_at=? WHERE username=?", (now, name))
    logic.add_daily_usage(total_delta)


def _sync_node_config(client, node):
    cfg = client.get_config() or {}
    general = cfg.get("general") or {}
    links = general.get("links") or {}
    cens = cfg.get("censorship") or {}
    want_tag = logic.node_ad_tag(node)
    patch = {}
    if (general.get("ad_tag") or logic.ZERO_TAG).lower() != want_tag:
        patch.setdefault("general", {})["ad_tag"] = want_tag
    if links.get("public_host") != node["public_host"] or int(links.get("public_port") or 0) != int(node["public_port"]):
        patch.setdefault("general", {})["links"] = {"public_host": node["public_host"], "public_port": int(node["public_port"])}
    reload_needed = False
    if cens.get("tls_domain") != node["tls_domain"]:
        patch["censorship"] = {"tls_domain": node["tls_domain"]}
        reload_needed = True
    if patch:
        client.patch_config(patch, reload=reload_needed)


def _sync_users(client, node, remote_users):
    nid = node["id"]
    users = [u for u in db.q("SELECT * FROM users") if logic.user_on_node(u, nid)]
    want = {u["username"]: u for u in users}
    remote = {r["username"]: r for r in remote_users}
    hashes = {r["username"]: r["pushed_hash"] for r in db.q("SELECT username,pushed_hash FROM node_users WHERE node_id=?", (nid,))}
    errors = []

    for name, u in want.items():
        desired = logic.desired_user_payload(u, node)
        h = logic.payload_hash(desired)
        try:
            if name not in remote:
                body = {"username": name, **{k: v for k, v in desired.items() if v is not None}}
                client.create_user(body)
            elif hashes.get(name) != h or not logic.remote_matches(remote[name], desired):
                client.patch_user(name, desired)
            else:
                continue
            with db.tx() as c:
                c.execute(
                    """INSERT INTO node_users(node_id,username,pushed_hash,updated_at) VALUES(?,?,?,?)
                       ON CONFLICT(node_id,username) DO UPDATE SET pushed_hash=excluded.pushed_hash""",
                    (nid, name, h, db.now()),
                )
        except NodeError as e:
            errors.append(f"{name}: {e}")

    for name in remote:
        if name.startswith(RESERVED_PREFIX) or name in want:
            continue
        try:
            client.delete_user(name)
            with db.tx() as c:
                c.execute("DELETE FROM node_users WHERE node_id=? AND username=?", (nid, name))
        except NodeError as e:
            errors.append(f"delete {name}: {e}")
    return errors


def _resolve(domain):
    if not domain:
        return None
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return ""


def sync_node(node_id):
    """Full reconcile of one node. Blocking; run in a worker thread."""
    lock = _lock_for(node_id)
    if not lock.acquire(blocking=False):
        return
    try:
        node = db.q1("SELECT * FROM nodes WHERE id=?", (node_id,))
        if not node or not node["enabled"]:
            return
        client = NodeClient(node)
        try:
            client.ping()
            info = client.system_info() or {}
            gates = client.gates() or {}
            summary = client.summary() or {}
            remote_users = client.users()
            _collect_usage(node, remote_users)
            _sync_node_config(client, node)
            errors = _sync_users(client, node, remote_users)
            online = sum(1 for r in remote_users if (r.get("current_connections") or 0) > 0
                         and not r["username"].startswith(RESERVED_PREFIX))
            conns = sum(int(r.get("current_connections") or 0) for r in remote_users)
            # One Telegram app opens several TCP connections, so "people online" = distinct client IPs.
            ips, ip_count = set(), 0
            for r in remote_users:
                if r["username"].startswith(RESERVED_PREFIX):
                    continue
                lst = r.get("active_unique_ips_list")
                if isinstance(lst, list):
                    ips.update(str(x) for x in lst)
                else:
                    ip_count += int(r.get("active_unique_ips") or 0)
            status = {
                "version": info.get("version"),
                "uptime": info.get("uptime_seconds"),
                "route_mode": gates.get("route_mode"),
                "use_middle_proxy": gates.get("use_middle_proxy"),
                "me_ready": gates.get("me_runtime_ready"),
                "reroute_active": gates.get("reroute_active"),
                "connections_total": summary.get("connections_total"),
                "online_users": online,
                "connections": conns,
                "users_online": len(ips) + ip_count,
                "dns_ip": _resolve(node.get("domain")),
            }
            with db.tx() as c:
                c.execute(
                    "UPDATE nodes SET last_seen=?, last_error=?, status_json=? WHERE id=?",
                    (db.now(), "; ".join(errors)[:1000] or None, json.dumps(status), node_id),
                )
        except NodeError as e:
            with db.tx() as c:
                c.execute("UPDATE nodes SET last_error=? WHERE id=?", (str(e)[:1000], node_id))
        except Exception as e:  # never kill the loop
            log.exception("sync node %s failed", node_id)
            with db.tx() as c:
                c.execute("UPDATE nodes SET last_error=? WHERE id=?", (f"internal: {e}"[:1000], node_id))
    finally:
        lock.release()


async def sync_all():
    nodes = db.q("SELECT id FROM nodes WHERE enabled=1")
    await asyncio.gather(*(asyncio.to_thread(sync_node, n["id"]) for n in nodes))


async def loop():
    global _wake, _loop
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    while True:
        try:
            await sync_all()
        except Exception:
            log.exception("sync pass failed")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=SYNC_INTERVAL)
            await asyncio.sleep(0.5)  # debounce bursts of edits
        except asyncio.TimeoutError:
            pass
        _wake.clear()
