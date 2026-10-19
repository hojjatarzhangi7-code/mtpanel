import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import auth, cloudflare, db, i18n, logic, sync, system
from .i18n import L
from .config import COOKIE_SECURE, DB_PATH, PANEL_PATH, SESSION_TTL
from .node_client import NodeClient, NodeError, drop_pool
from .sub import render_public, render_qr_svg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "mtp_session"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}$")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    auth.ensure_admin(os.environ.get("MTP_ADMIN_USER", ""), os.environ.get("MTP_ADMIN_PASS", ""),
                      force=os.environ.get("MTP_ADMIN_FORCE") == "1")
    system.forget_install_password()
    if not db.get_setting("setup_done") and db.q1("SELECT id FROM nodes"):
        db.set_setting("setup_done", "1")  # upgraded from a version without the setup wizard
    legacy = (db.get_setting("default_ad_tag") or "").lower()
    if legacy and logic.HEX32_RE.match(legacy) and not db.q1("SELECT id FROM ad_tags"):
        with db.tx() as c:
            tid = c.execute("INSERT INTO ad_tags(name,tag,created_at) VALUES(?,?,?)", ("کانال اصلی", legacy, db.now())).lastrowid
        db.set_setting("default_tag_id", str(tid))
        db.set_setting("default_ad_tag", "")
    if not db.get_setting("public_token"):
        db.set_setting("public_token", logic.new_sub_token())
    task = asyncio.create_task(sync.loop())
    yield
    task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
api = APIRouter(prefix=PANEL_PATH + "/api")


@app.middleware("http")
async def _ui_language(request: Request, call_next):
    i18n.set_request_lang(request.headers.get("X-Lang"))  # messages follow the browser's UI language
    return await call_next(request)


# ---------------------------------------------------------------- auth
def current_admin(request: Request):
    tok = request.cookies.get(COOKIE)
    admin = auth.check_session(tok) if tok else None
    if not admin:
        raise HTTPException(401, L("نیاز به ورود", "Login required"))
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "mtpanel":
        raise HTTPException(403, "CSRF")
    return admin


def _set_cookie(response, username, pw_hash):
    response.set_cookie(COOKIE, auth.make_session(username, pw_hash), max_age=SESSION_TTL, httponly=True,
                        samesite="strict", secure=COOKIE_SECURE, path=PANEL_PATH or "/")


class LoginIn(BaseModel):
    username: str
    password: str


@api.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if not auth.login_allowed(ip):
        raise HTTPException(429, L("تلاش زیاد؛ ۱۵ دقیقه بعد دوباره امتحان کنید", "Too many attempts; try again in 15 minutes"))
    admin = db.q1("SELECT * FROM admins WHERE username=?", (body.username,))
    if not admin or not auth.verify_password(body.password, admin["pw_hash"]):
        auth.login_failed(ip)
        raise HTTPException(401, L("نام کاربری یا رمز اشتباه است", "Wrong username or password"))
    auth.login_ok(ip)
    _set_cookie(response, admin["username"], admin["pw_hash"])
    return {"ok": True}


@api.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path=PANEL_PATH or "/")
    return {"ok": True}


@api.get("/me")
def me(admin=Depends(current_admin)):
    return {"username": admin["username"]}


class PwIn(BaseModel):
    current: str
    new: str
    username: Optional[str] = None


@api.post("/password")
def change_password(body: PwIn, response: Response, admin=Depends(current_admin)):
    if not auth.verify_password(body.current, admin["pw_hash"]):
        raise HTTPException(400, L("رمز فعلی اشتباه است", "Current password is wrong"))
    if len(body.new) < 8:
        raise HTTPException(400, L("رمز جدید باید حداقل ۸ کاراکتر باشد", "New password must be at least 8 characters"))
    new_user = (body.username or admin["username"]).strip()
    h = auth.hash_password(body.new)
    with db.tx() as c:
        c.execute("UPDATE admins SET username=?, pw_hash=? WHERE id=?", (new_user, h, admin["id"]))
    _set_cookie(response, new_user, h)
    return {"ok": True}


# ---------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    default_ad_tag: Optional[str] = ""
    panel_title: Optional[str] = ""
    cf_token: Optional[str] = None      # None = keep current
    public_enabled: Optional[bool] = True
    public_base_url: Optional[str] = None  # None = keep current
    public_lang: Optional[str] = None      # language of the public links page


@api.get("/settings")
def get_settings(admin=Depends(current_admin)):
    return {
        "default_ad_tag": db.get_setting("default_ad_tag", ""),
        "panel_title": db.get_setting("panel_title", "MTPanel"),
        "cf_token_set": bool(db.get_setting("cf_token")),
        "public_token": db.get_setting("public_token"),
        "public_enabled": db.get_setting("public_enabled", "1") == "1",
        "public_base_url": db.get_setting("public_base_url", ""),
        "public_lang": db.get_setting("public_lang", "fa"),
        "ui_lang": db.get_setting("ui_lang", "fa"),
    }


@api.put("/settings")
def put_settings(body: SettingsIn, admin=Depends(current_admin)):
    if body.cf_token is not None and body.cf_token.strip():
        try:
            cloudflare.verify_token(body.cf_token.strip())
        except cloudflare.CFError as e:
            raise HTTPException(400, L("توکن Cloudflare نامعتبر است: ", "Invalid Cloudflare token: ") + str(e))
        db.set_setting("cf_token", body.cf_token.strip())
    db.set_setting("panel_title", (body.panel_title or "MTPanel").strip()[:40])
    db.set_setting("public_enabled", "1" if body.public_enabled is not False else "0")
    if body.public_base_url is not None:
        base = body.public_base_url.strip().rstrip("/")
        if base and not re.match(r"^https?://[^\s/]+(:\d+)?$", base):
            raise HTTPException(400, L("آدرس باید مثل https://example.com یا https://example.com:2053 باشد",
                                        "Address must look like https://example.com or https://example.com:2053"))
        db.set_setting("public_base_url", base)
    if body.public_lang in i18n.LANGS:
        db.set_setting("public_lang", body.public_lang)
    sync.request_sync()
    return {"ok": True}


@api.delete("/settings/cf-token")
def delete_cf(admin=Depends(current_admin)):
    db.set_setting("cf_token", "")
    return {"ok": True}


@api.post("/settings/rotate-public")
def rotate_public(admin=Depends(current_admin)):
    db.set_setting("public_token", logic.new_sub_token())
    return {"ok": True}


class LangIn(BaseModel):
    lang: str


@api.post("/lang")
def set_ui_lang(body: LangIn, admin=Depends(current_admin)):
    if body.lang not in i18n.LANGS:
        raise HTTPException(400, "fa / en")
    db.set_setting("ui_lang", body.lang)  # used for messages produced by background jobs
    return {"ok": True}


# ---------------------------------------------------------------- ads (sponsor tags + campaigns)
class TagIn(BaseModel):
    name: str
    tag: str


class AdsSettingsIn(BaseModel):
    enabled: bool = True
    default_tag_id: Optional[int] = None


class CampaignIn(BaseModel):
    tag_id: int
    node_ids: Optional[List[int]] = None
    start_at: Optional[int] = None   # null = now
    duration_hours: float
    note: Optional[str] = ""


def _campaign_out(c, now):
    state = "running" if c["start_at"] <= now < c["end_at"] else ("upcoming" if now < c["start_at"] else "ended")
    return {**{k: c[k] for k in ("id", "tag_id", "start_at", "end_at", "note", "created_at")},
            "tag_name": c.get("tag_name"), "node_ids": logic.loads_ids(c["node_ids"]), "state": state}


@api.get("/ads")
def ads_overview(admin=Depends(current_admin)):
    now = db.now()
    tags = db.q("SELECT * FROM ad_tags ORDER BY id")
    camps = db.q("""SELECT c.*, t.name AS tag_name FROM ad_campaigns c LEFT JOIN ad_tags t ON t.id=c.tag_id
                    WHERE c.end_at > ? OR c.id IN (SELECT id FROM ad_campaigns ORDER BY id DESC LIMIT 10)
                    ORDER BY c.start_at DESC""", (now - 30 * 86400,))
    nodes = []
    for n in db.q("SELECT * FROM nodes ORDER BY id"):
        tag, source, name = logic.effective_node_ad(n)
        st = db.loads(n["status_json"], {})
        nodes.append({"id": n["id"], "name": n["name"], "enabled": bool(n["enabled"]), "source": source,
                      "tag_name": name, "has_tag": tag != logic.ZERO_TAG, "node_tag": n["ad_tag"] or "",
                      "sponsor_ok": st.get("route_mode") == "middle" and not st.get("reroute_active"),
                      "route_mode": st.get("route_mode")})
    did = db.get_setting("default_tag_id")
    return {"enabled": logic.ads_enabled(), "default_tag_id": int(did) if did else None,
            "tags": [{**t, "links": db.q1("SELECT COUNT(*) AS c FROM users WHERE lower(ad_tag)=?", (t["tag"],))["c"],
                      "servers": db.q1("SELECT COUNT(*) AS c FROM nodes WHERE lower(ad_tag)=?", (t["tag"],))["c"]} for t in tags],
            "campaigns": [_campaign_out(c, now) for c in camps], "nodes": nodes}


@api.put("/ads/settings")
def ads_settings(body: AdsSettingsIn, admin=Depends(current_admin)):
    if body.default_tag_id is not None and not db.q1("SELECT id FROM ad_tags WHERE id=?", (body.default_tag_id,)):
        raise HTTPException(400, L("تگ پیدا نشد", "Tag not found"))
    db.set_setting("ads_enabled", "1" if body.enabled else "0")
    db.set_setting("default_tag_id", str(body.default_tag_id) if body.default_tag_id else "")
    sync.request_sync()
    return {"ok": True}


def _tag_value(tag):
    t = (tag or "").strip().lower()
    if not logic.HEX32_RE.match(t):
        raise HTTPException(400, L("تگ باید دقیقاً ۳۲ کاراکتر هگز باشد (همان Proxy tag که @MTProxybot می‌دهد)",
                                    "The tag must be exactly 32 hex characters (the Proxy tag @MTProxybot gives you)"))
    return t


@api.post("/ads/tags")
def add_tag(body: TagIn, admin=Depends(current_admin)):
    t = _tag_value(body.tag)
    if db.q1("SELECT id FROM ad_tags WHERE tag=?", (t,)):
        raise HTTPException(400, L("این تگ قبلاً اضافه شده", "This tag was already added"))
    with db.tx() as c:
        tid = c.execute("INSERT INTO ad_tags(name,tag,created_at) VALUES(?,?,?)",
                        (body.name.strip()[:40] or L("بی‌نام", "Untitled"), t, db.now())).lastrowid
    if not db.get_setting("default_tag_id"):
        db.set_setting("default_tag_id", str(tid))
    sync.request_sync()
    return {"ok": True, "id": tid}


@api.patch("/ads/tags/{tid}")
def edit_tag(tid: int, body: TagIn, admin=Depends(current_admin)):
    old = db.q1("SELECT * FROM ad_tags WHERE id=?", (tid,))
    if not old:
        raise HTTPException(404, L("تگ پیدا نشد", "Tag not found"))
    t = _tag_value(body.tag)
    with db.tx() as c:
        c.execute("UPDATE ad_tags SET name=?, tag=? WHERE id=?", (body.name.strip()[:40] or old["name"], t, tid))
        if t != old["tag"]:  # keep servers/links that referenced the old value pointing at this entry
            c.execute("UPDATE nodes SET ad_tag=? WHERE lower(ad_tag)=?", (t, old["tag"]))
            c.execute("UPDATE users SET ad_tag=? WHERE lower(ad_tag)=?", (t, old["tag"]))
    sync.request_sync()
    return {"ok": True}


@api.delete("/ads/tags/{tid}")
def delete_tag(tid: int, admin=Depends(current_admin)):
    old = db.q1("SELECT * FROM ad_tags WHERE id=?", (tid,))
    if not old:
        raise HTTPException(404, L("تگ پیدا نشد", "Tag not found"))
    with db.tx() as c:
        c.execute("DELETE FROM ad_tags WHERE id=?", (tid,))
        c.execute("DELETE FROM ad_campaigns WHERE tag_id=?", (tid,))
        c.execute("UPDATE nodes SET ad_tag=NULL WHERE lower(ad_tag)=?", (old["tag"],))
        c.execute("UPDATE users SET ad_tag=NULL WHERE lower(ad_tag)=?", (old["tag"],))
    if db.get_setting("default_tag_id") == str(tid):
        db.set_setting("default_tag_id", "")
    sync.request_sync()
    return {"ok": True}


@api.post("/ads/campaigns")
def add_campaign(body: CampaignIn, admin=Depends(current_admin)):
    if not db.q1("SELECT id FROM ad_tags WHERE id=?", (body.tag_id,)):
        raise HTTPException(400, L("تگ پیدا نشد", "Tag not found"))
    if not (0 < body.duration_hours <= 24 * 365):
        raise HTTPException(400, L("مدت نامعتبر است", "Invalid duration"))
    now = db.now()
    start = max(now, int(body.start_at)) if body.start_at else now
    end = start + int(body.duration_hours * 3600)
    with db.tx() as c:
        cid = c.execute("""INSERT INTO ad_campaigns(tag_id,node_ids,start_at,end_at,note,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (body.tag_id, json.dumps(body.node_ids) if body.node_ids is not None else None,
                         start, end, (body.note or "")[:200], now)).lastrowid
    sync.request_sync()
    return {"ok": True, "id": cid}


@api.post("/ads/campaigns/{cid}/stop")
def stop_campaign(cid: int, admin=Depends(current_admin)):
    now = db.now()
    with db.tx() as c:
        c.execute("UPDATE ad_campaigns SET end_at=? WHERE id=? AND end_at>?", (now, cid, now))
        c.execute("UPDATE ad_campaigns SET start_at=end_at WHERE id=? AND start_at>end_at", (cid,))
    sync.request_sync()
    return {"ok": True}


@api.delete("/ads/campaigns/{cid}")
def delete_campaign(cid: int, admin=Depends(current_admin)):
    with db.tx() as c:
        c.execute("DELETE FROM ad_campaigns WHERE id=?", (cid,))
    sync.request_sync()
    return {"ok": True}


# ---------------------------------------------------------------- panel access (address, port, path, HTTPS)
@api.get("/system")
def system_info(admin=Depends(current_admin)):
    host = config_host()
    return {
        "managed": system.managed(),
        "url": system.url(host),
        "host": host, "port": system.PORT, "path": PANEL_PATH.strip("/"),
        "public_ip": system.PUBLIC_IP,
        "https": "domain" if host and not system.is_ip(host) else "self-signed",
        "certbot": bool(shutil.which("certbot")),
        "cert": system.cert_status(),
        "setup_done": db.get_setting("setup_done") == "1",
    }


def config_host():
    return system.HOST or system.PUBLIC_IP


def _need_managed():
    if not system.managed():
        raise HTTPException(400, L("این تنظیم فقط وقتی کار می‌کند که پنل با install.sh نصب شده باشد",
                                    "This setting only works when the panel was installed with install.sh"))


class AccessIn(BaseModel):
    port: int
    path: str


@api.post("/system/access")
def set_access(body: AccessIn, admin=Depends(current_admin)):
    _need_managed()
    path = body.path.strip().strip("/")
    if not system.PATH_RE.match(path):
        raise HTTPException(400, L("مسیر باید ۴ تا ۶۴ کاراکتر و فقط حروف انگلیسی، عدد، - و _ باشد",
                                    "Path must be 4-64 characters: English letters, digits, - and _"))
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, L("پورت نامعتبر است", "Invalid port"))
    if body.port == system.PORT and path == PANEL_PATH.strip("/"):
        raise HTTPException(400, L("تغییری داده نشده", "Nothing changed"))
    if body.port != system.PORT and not system.port_free(body.port):
        raise HTTPException(400, L(f"پورت {body.port} روی سرور اشغال است", f"Port {body.port} is already in use on the server"))
    system.write_env({"MTP_PORT": str(body.port), "MTP_PANEL_PATH": path})
    if body.port != system.PORT:
        system.open_firewall(body.port)
    db.set_setting("setup_done", "1")
    system.schedule_restart()
    return {"ok": True, "url": system.url(config_host(), body.port, path)}


class CertIn(BaseModel):
    domain: str
    force: Optional[bool] = False


@api.post("/system/certificate")
def get_certificate(body: CertIn, admin=Depends(current_admin)):
    _need_managed()
    domain = body.domain.strip().lower().rstrip(".")
    if not system.DOMAIN_RE.match(domain):
        raise HTTPException(400, L("دامنه نامعتبر است (مثال: panel.example.com)", "Invalid domain (example: panel.example.com)"))
    ips = system.resolve(domain)
    if not body.force and system.PUBLIC_IP and system.PUBLIC_IP not in ips:
        now = ", ".join(ips) if ips else L("هیچ IP‌ای", "no IP")
        # 412 = "DNS does not point here"; the page offers to try anyway
        raise HTTPException(412, L(f"دامنه‌ی {domain} الان به {now} اشاره می‌کند، نه به IP این سرور ({system.PUBLIC_IP}). "
                                   "اول در DNS یک رکورد A به این IP بسازید (اگر Cloudflare است، ابر خاکستری).",
                                   f"{domain} currently points to {now}, not to this server's IP ({system.PUBLIC_IP}). "
                                   "First create an A record to this IP in your DNS (grey cloud on Cloudflare)."))
    try:
        system.start_certificate(domain)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    db.set_setting("setup_done", "1")
    return {"ok": True, "url": system.url(domain)}


@api.get("/system/certificate")
def certificate_status(admin=Depends(current_admin)):
    return system.cert_status()


@api.post("/setup/done")
def setup_done(admin=Depends(current_admin)):
    db.set_setting("setup_done", "1")
    return {"ok": True}


# ---------------------------------------------------------------- dashboard
@api.get("/dashboard")
def dashboard(admin=Depends(current_admin)):
    links = db.q("SELECT enabled FROM users")
    nodes = [_node_out(n) for n in db.q("SELECT * FROM nodes ORDER BY id")]
    return {
        "links": len(links),
        "links_active": sum(1 for x in links if x["enabled"]),
        "connections": sum((n["status"] or {}).get("connections") or 0 for n in nodes if n["online"]),
        "users_online": sum((n["status"] or {}).get("users_online") or 0 for n in nodes if n["online"]),
        "nodes": nodes,
    }


# ---------------------------------------------------------------- links (stored in the users table)
class LinkIn(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = ""
    enabled: Optional[bool] = True
    ad_tag: Optional[str] = ""
    node_ids: Optional[List[int]] = None   # null = all servers
    count: Optional[int] = 1


def _link_out(u):
    nus = db.q("SELECT connections, active_ips FROM node_users WHERE username=?", (u["username"],))
    return {
        "id": u["id"], "name": u["username"], "note": u["note"], "enabled": bool(u["enabled"]),
        "ad_tag": u["ad_tag"] or "", "node_ids": logic.user_node_ids(u), "secret": u["secret"],
        "connections": sum(x["connections"] for x in nus), "users_online": sum(x["active_ips"] or 0 for x in nus),
        "created_at": u["created_at"],
    }


def _get_link(lid):
    u = db.q1("SELECT * FROM users WHERE id=?", (lid,))
    if not u:
        raise HTTPException(404, L("لینک پیدا نشد", "Link not found"))
    return u


def _clean_tag(tag):
    tag = (tag or "").strip().lower()
    if tag and not logic.HEX32_RE.match(tag):
        raise HTTPException(400, L("تگ اسپانسر باید ۳۲ کاراکتر هگز باشد", "Sponsor tag must be 32 hex characters"))
    return tag or None


@api.get("/links")
def list_links(admin=Depends(current_admin)):
    return [_link_out(u) for u in db.q("SELECT * FROM users ORDER BY id")]


@api.post("/links")
def create_links(body: LinkIn, admin=Depends(current_admin)):
    existing = {r["username"] for r in db.q("SELECT username FROM users")}
    base = (body.name or "").strip() or "link"
    count = max(1, min(int(body.count or 1), 200))
    names = []
    if count == 1 and base not in existing:
        names = [base]
    else:
        i = 1
        while len(names) < count:
            n = f"{base}{i}"
            if n not in existing:
                names.append(n)
            i += 1
    for n in names:
        if not logic.USERNAME_RE.match(n) or n.startswith("__"):
            raise HTTPException(400, L(f"نام نامعتبر: {n} (فقط حروف انگلیسی، عدد، _ . -)", f"Invalid name: {n} (English letters, digits, _ . - only)"))
    tag = _clean_tag(body.ad_tag)
    nodes_json = json.dumps(body.node_ids) if body.node_ids is not None else None
    with db.tx() as c:
        for n in names:
            c.execute(
                """INSERT INTO users(username,secret,note,enabled,ad_tag,node_ids,sub_token,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (n, logic.new_secret(), (body.note or "")[:200], 0 if body.enabled is False else 1,
                 tag, nodes_json, logic.new_sub_token(), db.now()))
    sync.request_sync()
    return {"ok": True, "created": names}


@api.patch("/links/{lid}")
def update_link(lid: int, body: LinkIn, admin=Depends(current_admin)):
    _get_link(lid)
    with db.tx() as c:
        c.execute("UPDATE users SET note=?, enabled=?, ad_tag=?, node_ids=? WHERE id=?",
                  ((body.note or "")[:200], 0 if body.enabled is False else 1, _clean_tag(body.ad_tag),
                   json.dumps(body.node_ids) if body.node_ids is not None else None, lid))
    sync.request_sync()
    return {"ok": True}


@api.delete("/links/{lid}")
def delete_link(lid: int, admin=Depends(current_admin)):
    _get_link(lid)
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (lid,))
    sync.request_sync()
    return {"ok": True}


@api.post("/links/{lid}/rotate-secret")
def rotate_secret(lid: int, admin=Depends(current_admin)):
    _get_link(lid)
    with db.tx() as c:
        c.execute("UPDATE users SET secret=? WHERE id=?", (logic.new_secret(), lid))
    sync.request_sync()
    return {"ok": True}


@api.get("/links/{lid}/urls")
def link_urls(lid: int, admin=Depends(current_admin)):
    u = _get_link(lid)
    return logic.user_links(u, db.q("SELECT * FROM nodes ORDER BY id"))


# ---------------------------------------------------------------- servers (nodes)
class NodeIn(BaseModel):
    name: str
    address: str
    agent_port: int = 7443
    token: Optional[str] = ""
    fingerprint: str
    domain: Optional[str] = ""
    public_port: Optional[int] = None
    tls_domain: Optional[str] = ""
    ad_tag: Optional[str] = ""
    enabled: Optional[bool] = True


def _node_out(n):
    now = db.now()
    return {
        "id": n["id"], "name": n["name"], "address": n["address"], "agent_port": n["agent_port"],
        "fingerprint": n["fingerprint"], "domain": n.get("domain") or "", "public_host": n["public_host"],
        "public_port": n["public_port"], "tls_domain": n["tls_domain"], "ad_tag": n["ad_tag"] or "",
        "enabled": bool(n["enabled"]), "last_seen": n["last_seen"], "last_error": n["last_error"],
        "status": db.loads(n["status_json"], {}), "ip_changed_at": n.get("ip_changed_at"),
        "online": bool(n["enabled"] and n["last_seen"] and now - n["last_seen"] < 120),
    }


def _valid_ip(ip):
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except ValueError:
        raise HTTPException(400, L(f"IP نامعتبر: {ip}", f"Invalid IP: {ip}"))


def _probe(address, port, token, fp):
    tmp = {"address": address, "agent_port": port, "token": token, "fingerprint": fp}
    client = NodeClient(tmp)
    try:
        client.ping()
        cfg = client.get_config() or {}
    except NodeError as e:
        drop_pool(tmp)
        raise HTTPException(400, L("اتصال به سرور ناموفق بود: ", "Could not connect to the server: ") + str(e))
    server = cfg.get("server") or {}
    links = (cfg.get("general") or {}).get("links") or {}
    return {"public_port": links.get("public_port") or server.get("port") or 443,
            "tls_domain": (cfg.get("censorship") or {}).get("tls_domain") or "www.google.com"}


def _node_fields(body: NodeIn, old=None):
    address = _valid_ip(body.address)
    token = (body.token or "").strip() or (old["token"] if old else "")
    if not token:
        raise HTTPException(400, L("توکن لازم است", "Token is required"))
    fp = body.fingerprint.replace(":", "").strip().lower()
    domain = (body.domain or "").strip().lower().rstrip(".")
    if domain and not DOMAIN_RE.match(domain):
        raise HTTPException(400, L("دامنه نامعتبر است", "Invalid domain"))
    probed, warning = None, None
    if body.enabled is not False:
        try:
            probed = _probe(address, int(body.agent_port), token, fp)
        except HTTPException as e:
            if not old:
                raise  # a new server must be reachable
            warning = str(e.detail)
    if probed is None:
        probed = {"public_port": old["public_port"] if old else 443,
                  "tls_domain": old["tls_domain"] if old else "www.google.com"}
    return warning, {
        "name": body.name.strip()[:40] or address, "address": address, "agent_port": int(body.agent_port),
        "token": token, "fingerprint": fp, "domain": domain or None, "public_host": domain or address,
        "public_port": int(body.public_port or probed["public_port"]),
        "tls_domain": (body.tls_domain or "").strip() or probed["tls_domain"],
        "ad_tag": _clean_tag(body.ad_tag), "enabled": 0 if body.enabled is False else 1,
    }


def _push_dns(domain, ip):
    token = db.get_setting("cf_token")
    if not domain or not token:
        return None
    try:
        cloudflare.set_a_record(token, domain, ip)
        return None
    except cloudflare.CFError as e:
        return str(e)


@api.get("/nodes")
def list_nodes(admin=Depends(current_admin)):
    return [_node_out(n) for n in db.q("SELECT * FROM nodes ORDER BY id")]


@api.post("/nodes")
def add_node(body: NodeIn, admin=Depends(current_admin)):
    _, f = _node_fields(body)
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO nodes(name,address,agent_port,token,fingerprint,domain,public_host,public_port,tls_domain,
               ad_tag,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f["name"], f["address"], f["agent_port"], f["token"], f["fingerprint"], f["domain"], f["public_host"],
             f["public_port"], f["tls_domain"], f["ad_tag"], f["enabled"], db.now()))
        nid = cur.lastrowid
    dns_err = _push_dns(f["domain"], f["address"])
    sync.request_sync()
    return {"ok": True, "id": nid, "dns_error": dns_err}


@api.patch("/nodes/{nid}")
def update_node(nid: int, body: NodeIn, admin=Depends(current_admin)):
    old = db.q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not old:
        raise HTTPException(404, L("سرور پیدا نشد", "Server not found"))
    warning, f = _node_fields(body, old)
    drop_pool(old)
    ip_changed = f["address"] != old["address"]
    with db.tx() as c:
        c.execute(
            """UPDATE nodes SET name=?,address=?,agent_port=?,token=?,fingerprint=?,domain=?,public_host=?,public_port=?,
               tls_domain=?,ad_tag=?,enabled=?,ip_changed_at=COALESCE(?,ip_changed_at) WHERE id=?""",
            (f["name"], f["address"], f["agent_port"], f["token"], f["fingerprint"], f["domain"], f["public_host"],
             f["public_port"], f["tls_domain"], f["ad_tag"], f["enabled"], db.now() if ip_changed else None, nid))
    dns_err = _push_dns(f["domain"], f["address"]) if (ip_changed or f["domain"] != old.get("domain")) else None
    sync.request_sync()
    return {"ok": True, "dns_error": dns_err, "warning": warning}


class ChangeIpIn(BaseModel):
    ip: str


@api.post("/nodes/{nid}/change-ip")
async def change_ip(nid: int, body: ChangeIpIn, admin=Depends(current_admin)):
    """Server got a new IP (old one filtered): repoint agent, DNS and links."""
    n = db.q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, L("سرور پیدا نشد", "Server not found"))
    ip = _valid_ip(body.ip)
    if ip == n["address"]:
        raise HTTPException(400, L("این همان IP فعلی است", "That is already the current IP"))
    domain = n.get("domain")
    dns_err = _push_dns(domain, ip) if domain else None
    drop_pool(n)
    with db.tx() as c:
        c.execute("UPDATE nodes SET address=?, public_host=?, ip_changed_at=? WHERE id=?",
                  (ip, domain or ip, db.now(), nid))
    reachable = await asyncio.to_thread(_reachable, db.q1("SELECT * FROM nodes WHERE id=?", (nid,)))
    if reachable:
        await asyncio.to_thread(sync.sync_node, nid)   # push the new link host right away
    else:
        sync.request_sync()                             # keep retrying in the background
    out = _node_out(db.q1("SELECT * FROM nodes WHERE id=?", (nid,)))
    return {"node": out, "reachable": reachable, "dns_error": dns_err, "links_changed": not domain,
            "cf_missing": bool(domain and not db.get_setting("cf_token"))}


def _reachable(node):
    try:
        NodeClient(node).ping()
        return True
    except NodeError:
        return False


@api.post("/nodes/{nid}/sync")
async def sync_now(nid: int, admin=Depends(current_admin)):
    await asyncio.to_thread(sync.sync_node, nid)
    n = db.q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, L("سرور پیدا نشد", "Server not found"))
    return _node_out(n)


@api.delete("/nodes/{nid}")
def delete_node(nid: int, admin=Depends(current_admin)):
    with db.tx() as c:
        c.execute("DELETE FROM nodes WHERE id=?", (nid,))
        c.execute("DELETE FROM node_users WHERE node_id=?", (nid,))
    return {"ok": True}


# ---------------------------------------------------------------- backup
@api.get("/backup")
def backup(admin=Depends(current_admin)):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    return FileResponse(path, filename=f"mtpanel-backup-{db.now()}.db", media_type="application/octet-stream",
                        background=BackgroundTask(os.remove, path))


app.include_router(api)


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- public page
def _public_groups():
    nodes = [n for n in db.q("SELECT * FROM nodes WHERE enabled=1 ORDER BY id")]
    links = db.q("SELECT * FROM users WHERE enabled=1 ORDER BY id")
    groups = []
    for n in nodes:
        items = [{"name": u["username"], "uid": u["id"], **logic.proxy_links(u, n)}
                 for u in links if logic.user_on_node(u, n["id"])]
        if items:
            groups.append({"node": n["name"], "nid": n["id"], "items": items})
    return groups


def _public_ok(token):
    return db.get_setting("public_enabled", "1") == "1" and token == db.get_setting("public_token")


@app.get("/p/{token}", response_class=HTMLResponse)
def public_page(token: str):
    if not _public_ok(token):
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(render_public(db.get_setting("panel_title", "MTPanel"), _public_groups(), token,
                                      db.get_setting("public_lang", "fa")),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/p/{token}/qr/{uid}/{nid}.svg")
def public_qr(token: str, uid: int, nid: int):
    u = db.q1("SELECT * FROM users WHERE id=? AND enabled=1", (uid,))
    n = db.q1("SELECT * FROM nodes WHERE id=? AND enabled=1", (nid,))
    if not _public_ok(token) or not u or not n or not logic.user_on_node(u, nid):
        return PlainTextResponse("Not Found", status_code=404)
    return Response(render_qr_svg(logic.proxy_links(u, n)["tg"]), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.get(PANEL_PATH + "/qr")
def admin_qr(text: str, admin=Depends(current_admin)):
    return Response(render_qr_svg(text[:2000]), media_type="image/svg+xml")


# ---------------------------------------------------------------- admin SPA
app.mount(PANEL_PATH + "/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/static-font", StaticFiles(directory=os.path.join(STATIC_DIR, "fonts"), check_dir=False), name="font")


def _asset_version():
    """Short hash of the UI files, appended as ?v= so browsers load new files right after an update."""
    h = hashlib.sha1()
    for name in ("i18n.js", "app.js", "style.css"):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


ASSET_VER = _asset_version()


def _index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html.replace("__BASE__", PANEL_PATH).replace("__VER__", ASSET_VER),
                        headers={"Cache-Control": "no-store"})


if PANEL_PATH:
    @app.get(PANEL_PATH, include_in_schema=False)
    def _panel_noslash():
        return _index()

    @app.get("/", include_in_schema=False)
    def _root():
        return PlainTextResponse("Not Found", status_code=404)


@app.get(PANEL_PATH + "/", include_in_schema=False)
def _panel_root():
    return _index()
