import os
import secrets

DATA_DIR = os.environ.get("MTP_DATA_DIR", "/var/lib/mtpanel")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "panel.db")
SYNC_INTERVAL = max(10, int(os.environ.get("MTP_SYNC_INTERVAL", "30")))


def _normalize_path(p):
    p = (p or "").strip().strip("/")
    return "/" + p if p else ""


# Secret URL prefix for the admin panel, e.g. "/k3x9qa" (empty = served at "/").
PANEL_PATH = _normalize_path(os.environ.get("MTP_PANEL_PATH", ""))

_key_file = os.path.join(DATA_DIR, "secret.key")
if not os.path.exists(_key_file):
    with open(_key_file, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(_key_file, 0o600)
with open(_key_file) as f:
    SECRET_KEY = f.read().strip().encode()

# Set by install.sh; when present the panel can change its own address/port/certificate from the web.
ENV_FILE = os.environ.get("MTP_ENV_FILE", "")
CERT_DIR = os.environ.get("MTP_CERT_DIR", "")
PORT = int(os.environ.get("MTP_PORT", "0") or 0)
HOST = os.environ.get("MTP_HOST", "")
PUBLIC_IP = os.environ.get("MTP_PUBLIC_IP", "")

SESSION_TTL = 7 * 24 * 3600
COOKIE_SECURE = os.environ.get("MTP_COOKIE_SECURE", "1") == "1"
