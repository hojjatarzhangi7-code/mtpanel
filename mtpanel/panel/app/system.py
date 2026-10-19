"""Panel self-management: address/port/path and HTTPS certificate, changed from the web.

install.sh writes the panel settings to an env file (MTP_ENV_FILE) that systemd loads.
Changing a setting = rewrite that file + ask systemd to restart the panel.
"""
import errno
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from . import i18n
from .config import CERT_DIR, ENV_FILE, HOST, PANEL_PATH, PORT, PUBLIC_IP
from .i18n import L

log = logging.getLogger("mtpanel.system")
PATH_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}$")


def managed():
    return bool(ENV_FILE) and os.path.isfile(ENV_FILE)


# ---------------------------------------------------------------- env file
def read_env():
    out = {}
    if not managed():
        return out
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(updates=None, remove=()):
    """Atomically update KEY=VALUE lines, keeping everything else as is."""
    updates = dict(updates or {})
    lines, seen = [], set()
    for line in open(ENV_FILE, encoding="utf-8").read().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remove:
            continue
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_FILE)


def forget_install_password():
    """The installer passes the first admin password via the env file; drop it once applied."""
    if managed():
        env = read_env()
        if "MTP_ADMIN_PASS" in env or "MTP_ADMIN_FORCE" in env:
            write_env(remove=("MTP_ADMIN_PASS", "MTP_ADMIN_FORCE"))


# ---------------------------------------------------------------- helpers
def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def url(host=None, port=None, path=None):
    host = host or HOST or PUBLIC_IP or "SERVER-IP"
    port = port or PORT
    path = PANEL_PATH.strip("/") if path is None else path
    return f"https://{host}:{port}/{path + '/' if path else ''}"


def port_free(port):
    """True if nothing listens on this TCP port (IPv4, and IPv6 when the host has it)."""
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue  # this address family isn't available on the host (e.g. no IPv6)
        try:
            if fam == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
        except OSError as e:
            if fam == socket.AF_INET6 and e.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            return False
        finally:
            s.close()
    return True


def open_firewall(port):
    if not shutil.which("ufw"):
        return
    try:
        st = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=10).stdout
        if "Status: active" in st:
            subprocess.run(["ufw", "allow", f"{port}/tcp"], capture_output=True, timeout=10)
    except Exception:
        log.warning("could not update ufw for port %s", port)


def schedule_restart(delay=1.5):
    """Restart the panel after the HTTP response has been sent."""
    def run():
        time.sleep(delay)
        try:
            r = subprocess.run(["systemctl", "--no-block", "restart", "mtpanel"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return
        except Exception:
            pass
        os._exit(3)  # systemd's Restart=always brings the panel back with the new settings

    threading.Thread(target=run, daemon=True).start()


def resolve(domain):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except Exception:
        return []


# ---------------------------------------------------------------- HTTPS certificate (Let's Encrypt)
_cert = {"state": "idle", "message": "", "domain": ""}
_cert_lock = threading.Lock()


def cert_status():
    return dict(_cert)


def start_certificate(domain):
    with _cert_lock:
        if _cert["state"] == "running":
            raise RuntimeError(L("در حال گرفتن گواهی هستیم؛ چند لحظه صبر کنید",
                                 "A certificate request is already running; please wait"))
        _cert.update(state="running", message=L("در حال گرفتن گواهی از Let's Encrypt…",
                                                "Requesting a certificate from Let's Encrypt…"), domain=domain)
    threading.Thread(target=_issue, args=(domain, i18n.lang()), daemon=True).start()


def _fail(msg):
    _cert.update(state="error", message=msg)


def _issue(domain, lang):
    i18n.set_request_lang(lang)  # report results in the language of whoever started the job
    try:
        if not shutil.which("certbot"):
            return _fail(L("certbot روی سرور نصب نیست. یک بار روی سرور بزنید: apt install -y certbot",
                           "certbot is not installed. Run once on the server: apt install -y certbot"))
        if not port_free(80):
            return _fail(L("پورت 80 روی سرور اشغال است. برای گرفتن گواهی، پورت 80 باید آزاد باشد.",
                           "Port 80 is in use on the server. It must be free to get a certificate."))
        r = subprocess.run(
            ["certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos",
             "--register-unsafely-without-email", "--preferred-challenges", "http", "-d", domain],
            capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            text = (r.stderr or "") + "\n" + (r.stdout or "")
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("Saving debug log")]
            detail = " | ".join(lines[-4:])[:600]
            return _fail(L("Let's Encrypt گواهی نداد. معمولاً یعنی دامنه هنوز به IP این سرور اشاره نمی‌کند یا پورت 80 در فایروال دیتاسنتر بسته است. جزئیات: ",
                           "Let's Encrypt refused. Usually the domain does not point to this server yet, or port 80 is closed in the datacenter firewall. Details: ") + detail)
        live = f"/etc/letsencrypt/live/{domain}"
        for src, dst in (("fullchain.pem", "panel.crt"), ("privkey.pem", "panel.key")):
            target = os.path.join(CERT_DIR, dst)
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(os.path.join(live, src), target)
        hooks = "/etc/letsencrypt/renewal-hooks/deploy"
        if os.path.isdir("/etc/letsencrypt"):
            os.makedirs(hooks, exist_ok=True)
            hook = os.path.join(hooks, "mtpanel.sh")
            with open(hook, "w") as f:
                f.write("#!/bin/sh\nsystemctl restart mtpanel\n")
            os.chmod(hook, 0o755)
        write_env({"MTP_HOST": domain})
        _cert.update(state="ok", message=L("گواهی گرفته شد. پنل با آدرس جدید دوباره راه‌اندازی می‌شود…",
                                           "Certificate issued. The panel is restarting on its new address…"))
        schedule_restart(2.0)
    except Exception as e:  # never leave the job stuck in "running"
        log.exception("certificate job failed")
        _fail(L("خطای داخلی: ", "Internal error: ") + str(e))
