"""Minimal Cloudflare DNS client (stdlib only) to repoint an A record on IP change."""
import json
import urllib.error
import urllib.parse
import urllib.request

from .i18n import L

API = "https://api.cloudflare.com/client/v4"


class CFError(Exception):
    pass


def _call(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            raise CFError(f"Cloudflare HTTP {e.code}")
    except Exception as e:
        raise CFError(L(f"اتصال به Cloudflare ممکن نیست: {e.__class__.__name__}", f"Cannot reach Cloudflare: {e.__class__.__name__}"))
    if not payload.get("success"):
        errs = payload.get("errors") or [{}]
        raise CFError("Cloudflare: " + "; ".join(str(x.get("message", x)) for x in errs))
    return payload.get("result")


def _zone_id(token, record):
    parts = record.strip(".").split(".")
    for i in range(len(parts) - 1):
        name = ".".join(parts[i:])
        res = _call(token, "GET", "/zones?" + urllib.parse.urlencode({"name": name}))
        if res:
            return res[0]["id"]
    raise CFError(L(f"زون Cloudflare برای {record} پیدا نشد (توکن به این دامنه دسترسی دارد؟)",
                   f"No Cloudflare zone found for {record} (does the token have access to this domain?)"))


def set_a_record(token, record, ip):
    """Create or update `record` as an A record pointing to `ip` (DNS only, not proxied)."""
    if not token:
        raise CFError(L("توکن Cloudflare در تنظیمات وارد نشده", "No Cloudflare token is set in Settings"))
    zid = _zone_id(token, record)
    q = urllib.parse.urlencode({"type": "A", "name": record})
    existing = _call(token, "GET", f"/zones/{zid}/dns_records?{q}") or []
    body = {"type": "A", "name": record, "content": ip, "ttl": 60, "proxied": False}
    if existing:
        _call(token, "PUT", f"/zones/{zid}/dns_records/{existing[0]['id']}", body)
        for extra in existing[1:]:  # keep exactly one A record
            _call(token, "DELETE", f"/zones/{zid}/dns_records/{extra['id']}")
    else:
        _call(token, "POST", f"/zones/{zid}/dns_records", body)
    return True


def verify_token(token):
    try:
        _call(token, "GET", "/user/tokens/verify")
    except CFError as first:
        # Account-owned tokens can't use /user/tokens/verify; listing zones proves the token works.
        try:
            _call(token, "GET", "/zones?per_page=1")
        except CFError:
            raise first
    return True
