"""Shared domain helpers: user status, desired telemt state, proxy links."""
import datetime as dt
import hashlib
import json
import re
import secrets
import urllib.parse

from . import db

ZERO_TAG = "0" * 32
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def new_secret():
    return secrets.token_hex(16)


def new_sub_token():
    return secrets.token_urlsafe(18)


def user_status(u, now=None):
    now = now or db.now()
    if not u["enabled"]:
        return "disabled"
    if u["expire_at"] and now >= u["expire_at"]:
        return "expired"
    if u["quota_bytes"] and u["used_bytes"] >= u["quota_bytes"]:
        return "limited"
    return "active"


def user_node_ids(u):
    ids = db.loads(u.get("node_ids"))
    return None if ids is None else [int(x) for x in ids]


def user_on_node(u, node_id):
    ids = user_node_ids(u)
    return ids is None or node_id in ids


def desired_user_payload(u, node=None):
    """What telemt should hold for this user (identical on every node)."""
    exp = None
    if u["expire_at"]:
        exp = dt.datetime.fromtimestamp(u["expire_at"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "secret": u["secret"],
        # An active campaign on this node overrides per-link tags so the advertiser is shown everywhere.
        "user_ad_tag": None if (node and active_campaign(node)) or not ads_enabled() else (u["ad_tag"] or None),
        "max_tcp_conns": u["max_conns"] or None,
        "expiration_rfc3339": exp,
        "data_quota_bytes": None,  # quota is enforced globally by the panel across all nodes
        "rate_limit_up_bps": u["rate_up_bps"] or None,
        "rate_limit_down_bps": u["rate_down_bps"] or None,
        "max_unique_ips": u["max_ips"] or None,
        "enabled": user_status(u) == "active",
    }


def payload_hash(p):
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()


def remote_matches(remote, desired):
    """Compare the fields telemt reports back (it never returns the secret)."""
    def norm_exp(v):
        if not v:
            return None
        try:
            return int(dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except Exception:
            return v

    for k in ("user_ad_tag", "max_tcp_conns", "rate_limit_up_bps", "rate_limit_down_bps",
              "max_unique_ips", "enabled", "data_quota_bytes"):
        rv = remote.get(k)
        dv = desired.get(k)
        if k == "user_ad_tag":
            rv = (rv or "").lower() or None
            dv = (dv or "").lower() or None
        if rv != dv:
            return False
    return norm_exp(remote.get("expiration_rfc3339")) == norm_exp(desired.get("expiration_rfc3339"))


def ads_enabled():
    return db.get_setting("ads_enabled", "1") == "1"


def active_campaign(node, now=None):
    """Most recently created campaign currently running on this node, if any."""
    now = now or db.now()
    for c in db.q("""SELECT c.*, t.tag, t.name AS tag_name FROM ad_campaigns c JOIN ad_tags t ON t.id=c.tag_id
                     WHERE c.start_at<=? AND c.end_at>? ORDER BY c.id DESC""", (now, now)):
        ids = loads_ids(c["node_ids"])
        if ids is None or node["id"] in ids:
            return c
    return None


def loads_ids(s):
    v = db.loads(s)
    return None if v is None else [int(x) for x in v]


def tag_name(tag):
    r = db.q1("SELECT name FROM ad_tags WHERE tag=?", ((tag or "").lower(),))
    return r["name"] if r else None


def effective_node_ad(node):
    """(tag, source, name) actually applied as the node-wide sponsor tag."""
    if not ads_enabled():
        return ZERO_TAG, "off", None
    c = active_campaign(node)
    if c:
        return c["tag"].lower(), "campaign", c["tag_name"]
    if node.get("ad_tag") and HEX32_RE.match(node["ad_tag"]):
        return node["ad_tag"].lower(), "node", tag_name(node["ad_tag"])
    did = db.get_setting("default_tag_id")
    if did:
        t = db.q1("SELECT * FROM ad_tags WHERE id=?", (int(did),))
        if t:
            return t["tag"].lower(), "default", t["name"]
    return ZERO_TAG, "none", None


def node_ad_tag(node):
    return effective_node_ad(node)[0]


def tls_secret(secret, tls_domain):
    return "ee" + secret + tls_domain.encode().hex()


def proxy_links(u, node):
    s = tls_secret(u["secret"], node["tls_domain"])
    q = urllib.parse.urlencode({"server": node["public_host"], "port": node["public_port"], "secret": s})
    return {"tg": f"tg://proxy?{q}", "https": f"https://t.me/proxy?{q}"}


def user_links(u, nodes):
    out = []
    for n in nodes:
        if n["enabled"] and user_on_node(u, n["id"]):
            out.append({"node_id": n["id"], "node": n["name"], **proxy_links(u, n)})
    return out


def add_daily_usage(delta):
    if delta <= 0:
        return
    day = dt.date.today().isoformat()
    with db.tx() as c:
        c.execute(
            "INSERT INTO usage_daily(day,bytes) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes",
            (day, delta),
        )
