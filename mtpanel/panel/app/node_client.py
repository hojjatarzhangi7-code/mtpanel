"""HTTPS client for a node agent, with SHA-256 certificate pinning."""
import json
import urllib.parse

import urllib3

from .i18n import L

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_pools = {}


class NodeError(Exception):
    def __init__(self, msg, code=None, status=None):
        super().__init__(msg)
        self.code = code
        self.status = status


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


def _pool(node):
    key = (node["address"], int(node["agent_port"]), _norm_fp(node["fingerprint"]))
    p = _pools.get(key)
    if p is None:
        p = urllib3.HTTPSConnectionPool(
            node["address"],
            port=int(node["agent_port"]),
            cert_reqs="CERT_NONE",           # self-signed; trust comes from the pin below
            assert_hostname=False,
            assert_fingerprint=_norm_fp(node["fingerprint"]),
            maxsize=4,
            timeout=urllib3.Timeout(connect=8, read=25),
            retries=False,
        )
        _pools[key] = p
    return p


def drop_pool(node):
    key = (node["address"], int(node["agent_port"]), _norm_fp(node["fingerprint"]))
    p = _pools.pop(key, None)
    if p:
        p.close()


class NodeClient:
    def __init__(self, node):
        self.node = node

    def call(self, method, path, body=None, params=None):
        url = path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"X-Agent-Token": self.node["token"]}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            r = _pool(self.node).request(method, url, body=data, headers=headers)
        except urllib3.exceptions.SSLError as e:
            if "fingerprint" in str(e).lower():
                raise NodeError(L("اثر انگشت گواهی نود مطابقت ندارد", "Node certificate fingerprint does not match"), "fingerprint")
            raise NodeError(f"TLS error: {e}", "tls")
        except Exception as e:
            raise NodeError(L(f"اتصال به نود ممکن نیست: {e.__class__.__name__}", f"Cannot reach the node: {e.__class__.__name__}"), "unreachable")
        try:
            payload = json.loads(r.data.decode() or "{}")
        except Exception:
            raise NodeError(L(f"پاسخ نامعتبر از نود (HTTP {r.status})", f"Invalid response from the node (HTTP {r.status})"), "bad_response", r.status)
        if r.status == 404 and path == "/agent/ping":
            raise NodeError(L("توکن اشتباه است یا IP پنل در لیست مجاز ایجنت نیست", "Wrong token, or the panel IP is not allowed by the agent"), "auth", 404)
        if not payload.get("ok"):
            err = payload.get("error") or {}
            raise NodeError(err.get("message") or err.get("code") or f"HTTP {r.status}", err.get("code"), r.status)
        return payload.get("data")

    # convenience wrappers
    def ping(self):
        return self.call("GET", "/agent/ping")

    def users(self):
        return self.call("GET", "/v1/users") or []

    def create_user(self, body):
        return self.call("POST", "/v1/users", body)

    def patch_user(self, username, body):
        return self.call("PATCH", f"/v1/users/{urllib.parse.quote(username)}", body)

    def delete_user(self, username):
        return self.call("DELETE", f"/v1/users/{urllib.parse.quote(username)}")

    def get_config(self):
        return self.call("GET", "/v1/config")

    def patch_config(self, body, reload=None):
        params = {"reload": "drain", "timeout_secs": 60} if reload else None
        return self.call("PATCH", "/v1/config", body, params)

    def system_info(self):
        return self.call("GET", "/v1/system/info")

    def gates(self):
        return self.call("GET", "/v1/runtime/gates")

    def summary(self):
        return self.call("GET", "/v1/stats/summary")
