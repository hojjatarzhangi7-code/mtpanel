#!/usr/bin/env bash
# MTPanel installer — Telegram MTProto (telemt) proxy panel
#
#   bash install.sh          -> menu: 1) main panel   2) node (proxy server)   (+ management items)
#   bash install.sh panel    -> install / update the main panel (asks only admin username + password)
#   bash install.sh node     -> install / update a node (asks nothing, prints a connection code)
#   After installing, the same menu is available anywhere with:  mtpanel
#
# Optional overrides for the node (normally not needed):
#   MTP_NODE_IP=1.2.3.4  MTP_NODE_PORT=443  MTP_NODE_TLS_DOMAIN=www.google.com  MTP_AGENT_PORT=7443
set -euo pipefail

REPO_URL="${MTP_REPO_URL:-https://github.com/YOUR_GITHUB/mtpanel}"
PANEL_DIR="/opt/mtpanel"            # panel: venv, data, certs, panel.env
PANEL_SRC="/opt/mtpanel/src"        # panel: source code
NODE_SRC="/opt/mtpanel-node"        # node: installer + agent (only what a node needs)
NODE_DIR="/etc/mtpanel-node"        # node: agent.env + agent certificate
TELEMT_CFG_DIR="/etc/telemt"
MGMT_CMD="/usr/local/bin/mtpanel"

c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
say()  { echo -e "${c_g}==>${c_0} $*"; }
warn() { echo -e "${c_y}!!${c_0} $*"; }
die()  { echo -e "${c_r}xx${c_0} $*" >&2; exit 1; }
# ask "Prompt" "default" -> reads from the terminal even when this script is piped; default when there is no terminal
ask() {
  local p="$1" d="${2:-}" v=""
  if (exec </dev/tty) 2>/dev/null; then read -r -p "$p${d:+ [$d]}: " v </dev/tty || true; fi
  echo "${v:-$d}"
}

[[ $EUID -eq 0 ]] || die "Run as root (sudo -i)"
command -v systemctl >/dev/null || die "systemd is required"

# NOTE: no `tr </dev/urandom | head` — under `set -o pipefail` that pipeline exits 141 (SIGPIPE).
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
rand_str() { local s; s=$(head -c 2048 /dev/urandom | tr -dc 'A-Za-z0-9'); printf '%s' "${s:0:$1}"; }
is_ipv4()  { [[ $1 =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; }

public_ip() {
  local ip u
  for u in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
    if ip=$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]') && is_ipv4 "$ip"; then
      echo "$ip"; return 0
    fi
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}
port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }
first_free_port() { local p; for p in "$@"; do port_busy "$p" || { echo "$p"; return 0; }; done; return 1; }
open_port() {
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then ufw allow "$1"/tcp >/dev/null || true; fi
  return 0
}
panel_installed() { [[ -f "$PANEL_DIR/panel.env" ]]; }
node_installed()  { [[ -f "$NODE_DIR/agent.env" ]]; }
script_dir() { cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true; }

base_pkgs() {
  say "Installing prerequisites..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || warn "apt-get update failed; continuing"
  apt-get install -y -qq curl openssl ca-certificates tar iproute2 python3 >/dev/null
}

missing_files() {
  if [[ -n "${MTP_REPO_URL:-}" && "$REPO_URL" != *YOUR_GITHUB* ]]; then
    apt-get install -y -qq git >/dev/null
    local tmp; tmp=$(mktemp -d)
    git clone -q --depth 1 "$REPO_URL" "$tmp/mtpanel"
    exec bash "$tmp/mtpanel/install.sh" "$@"
  fi
  die "Project files not found. Extract mtpanel.zip and run: cd mtpanel && bash install.sh"
}

install_mgmt_cmd() {
  cat >"$MGMT_CMD" <<'EOF'
#!/bin/sh
# MTPanel management menu
if [ -f /opt/mtpanel/src/install.sh ]; then exec bash /opt/mtpanel/src/install.sh "$@"; fi
if [ -f /opt/mtpanel-node/install.sh ]; then exec bash /opt/mtpanel-node/install.sh "$@"; fi
echo "MTPanel is not installed on this server"; exit 1
EOF
  chmod 755 "$MGMT_CMD"
}
remove_mgmt_cmd_if_unused() {
  panel_installed || node_installed || rm -f "$MGMT_CMD"
  return 0
}

# ============================================================== NODE
copy_node_src() {
  local here; here=$(script_dir)
  if [[ -n "$here" && -f "$here/node/agent.py" ]]; then
    if [[ "$here" != "$NODE_SRC" ]]; then
      mkdir -p "$NODE_SRC/node"
      install -m 644 "$here/node/agent.py" "$NODE_SRC/node/agent.py"
      install -m 755 "$here/install.sh" "$NODE_SRC/install.sh"
    fi
  elif [[ ! -f "$NODE_SRC/node/agent.py" ]]; then
    missing_files node
  fi
}

download_telemt() {  # $1=arch $2=libc -> installs /usr/local/bin/telemt
  local url tmp
  url="https://github.com/telemt/telemt/releases/latest/download/telemt-$1-linux-$2.tar.gz"
  tmp=$(mktemp -d)
  if ! curl -fsSL --retry 3 "$url" -o "$tmp/t.tgz"; then rm -rf "$tmp"; return 1; fi
  tar -xzf "$tmp/t.tgz" -C "$tmp" && install -m 755 "$tmp/telemt" /usr/local/bin/telemt
  rm -rf "$tmp"
}

install_telemt_bin() {
  local arch
  case "$(uname -m)" in
    x86_64|amd64) arch=x86_64;;
    aarch64|arm64) arch=aarch64;;
    *) die "Unsupported CPU: $(uname -m)";;
  esac
  say "Downloading telemt ($arch)..."
  # glibc build first (needs glibc >= 2.34); fall back to the static musl build on older systems.
  if download_telemt "$arch" gnu && /usr/local/bin/telemt --version >/dev/null 2>&1; then :
  elif download_telemt "$arch" musl && /usr/local/bin/telemt --version >/dev/null 2>&1; then :
  else die "Could not install telemt (is github.com reachable?)"; fi
  say "$(/usr/local/bin/telemt --version 2>/dev/null | head -1)"
}

install_node() {
  base_pkgs
  copy_node_src

  if node_installed; then
    say "Updating node (telemt + agent)..."
    install_telemt_bin
    install -m 755 "$NODE_SRC/node/agent.py" /usr/local/lib/mtpanel-agent.py
    systemctl daemon-reload
    systemctl restart telemt mtpanel-agent || true
    sleep 2
    systemctl is-active --quiet telemt || warn "telemt is not running: journalctl -u telemt -n 50 --no-pager"
    install_mgmt_cmd
    print_conn_code
    return 0
  fi

  local ip port tls agent_port
  # --- everything is automatic; the panel can change the link address / TLS domain later
  if [[ -n "${MTP_NODE_IP:-}" ]]; then ip=$MTP_NODE_IP; else ip=$(public_ip); fi
  is_ipv4 "$ip" || die "Could not detect this server's public IPv4. Run: MTP_NODE_IP=1.2.3.4 bash install.sh node"
  if [[ -n "${MTP_NODE_PORT:-}" ]]; then
    port=$MTP_NODE_PORT
    port_busy "$port" && die "Port $port is already in use"
  else
    port=$(first_free_port 443 8443 2083 2087 2096 9443 10443) || die "No free port for the proxy"
  fi
  if [[ -n "${MTP_AGENT_PORT:-}" ]]; then
    agent_port=$MTP_AGENT_PORT
    port_busy "$agent_port" && die "Port $agent_port is already in use"
  else
    # shellcheck disable=SC2046
    agent_port=$(first_free_port $(seq 7443 7499)) || die "No free port for the agent"
  fi
  tls="${MTP_NODE_TLS_DOMAIN:-www.google.com}"
  say "IP: $ip   proxy port: $port   agent port: $agent_port"
  [[ $port == 443 ]] || warn "Port 443 is busy, so the proxy will use port $port"

  install_telemt_bin
  id telemt >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /var/lib/telemt telemt
  mkdir -p "$TELEMT_CFG_DIR" /var/lib/telemt "$NODE_DIR"
  local api_auth agent_token reserved
  api_auth=$(rand_str 40)
  agent_token=$(rand_str 40)
  reserved=$(rand_hex 16)
  [[ ${#api_auth} -eq 40 && ${#agent_token} -eq 40 && ${#reserved} -eq 32 ]] || die "Could not generate secrets"

  cat >"$TELEMT_CFG_DIR/telemt.toml" <<EOF
# Managed by MTPanel. Links (users) are pushed by the panel through the local API.
[general]
use_middle_proxy = true
log_level = "normal"
ad_tag = "00000000000000000000000000000000"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
show = "*"
public_host = "$ip"
public_port = $port

[server]
port = $port

[server.api]
enabled = true
listen = "127.0.0.1:9091"
whitelist = ["127.0.0.1/32", "::1/128"]
auth_header = "$api_auth"

[[server.listeners]]
ip = "0.0.0.0"

[censorship]
tls_domain = "$tls"
mask = true
tls_emulation = true
tls_front_dir = "tlsfront"

[access.users]
__mtpanel = "$reserved"

[access.user_enabled]
__mtpanel = false
EOF
  chown -R telemt:telemt "$TELEMT_CFG_DIR" /var/lib/telemt
  chmod 700 "$TELEMT_CFG_DIR"
  chmod 600 "$TELEMT_CFG_DIR/telemt.toml"

  cat >/etc/systemd/system/telemt.service <<EOF
[Unit]
Description=telemt MTProto proxy (MTPanel node)
After=network-online.target
Wants=network-online.target

[Service]
User=telemt
Group=telemt
WorkingDirectory=/var/lib/telemt
ExecStart=/usr/local/bin/telemt $TELEMT_CFG_DIR/telemt.toml
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
LimitNOFILE=262144
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$NODE_DIR/agent.key" -out "$NODE_DIR/agent.crt" -days 3650 -subj "/CN=mtpanel-node" 2>/dev/null
  chmod 600 "$NODE_DIR/agent.key"
  install -m 755 "$NODE_SRC/node/agent.py" /usr/local/lib/mtpanel-agent.py
  cat >"$NODE_DIR/agent.env" <<EOF
AGENT_PORT=$agent_port
AGENT_TOKEN=$agent_token
TELEMT_API=http://127.0.0.1:9091
TELEMT_AUTH=$api_auth
AGENT_CERT=$NODE_DIR/agent.crt
AGENT_KEY=$NODE_DIR/agent.key
AGENT_ALLOW_IPS=
AGENT_PUBLIC_IP=$ip
EOF
  chmod 600 "$NODE_DIR/agent.env"
  cat >/etc/systemd/system/mtpanel-agent.service <<EOF
[Unit]
Description=MTPanel node agent
After=network-online.target telemt.service

[Service]
EnvironmentFile=$NODE_DIR/agent.env
ExecStart=/usr/bin/python3 /usr/local/lib/mtpanel-agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now telemt mtpanel-agent >/dev/null 2>&1 || true
  open_port "$port"
  open_port "$agent_port"
  sleep 3
  systemctl is-active --quiet telemt || warn "telemt is not running: journalctl -u telemt -n 50 --no-pager"
  systemctl is-active --quiet mtpanel-agent || warn "agent is not running: journalctl -u mtpanel-agent -n 50 --no-pager"
  install_mgmt_cmd
  print_conn_code
  echo "If your datacenter has a firewall, open TCP ports $port and $agent_port there too."
}

print_conn_code() {
  node_installed || die "No node on this server"
  # shellcheck disable=SC1091
  source "$NODE_DIR/agent.env"
  local fp json code
  fp=$(openssl x509 -in "$AGENT_CERT" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')
  json=$(printf '{"a":"%s","p":%s,"t":"%s","f":"%s","n":"%s"}' "$AGENT_PUBLIC_IP" "$AGENT_PORT" "$AGENT_TOKEN" "$fp" "$(hostname -s)")
  code="mtpnode://$(printf '%s' "$json" | base64 -w0 | tr '+/' '-_')"
  echo
  echo "${c_b}✅ Node is ready${c_0}"
  echo "Paste this connection code in the panel: Servers > Add server"
  echo
  echo "${c_g}$code${c_0}"
  echo
  echo "Show it again later: mtpanel"
}

uninstall_node() {
  [[ "$(ask "Uninstall the node? (y/N)" "n")" =~ ^[Yy] ]] || { echo "Cancelled"; return 0; }
  systemctl disable --now telemt mtpanel-agent 2>/dev/null || true
  rm -f /etc/systemd/system/telemt.service /etc/systemd/system/mtpanel-agent.service /usr/local/lib/mtpanel-agent.py
  rm -f "$NODE_DIR/agent.env"
  rm -rf "$NODE_SRC"
  systemctl daemon-reload
  remove_mgmt_cmd_if_unused
  warn "Node removed. telemt config kept in $TELEMT_CFG_DIR"
}

# ============================================================== PANEL
copy_panel_src() {
  local here; here=$(script_dir)
  if [[ -n "$here" && -f "$here/panel/app/main.py" ]]; then
    if [[ "$here" != "$PANEL_SRC" ]]; then
      mkdir -p "$PANEL_SRC"
      cp -a "$here/." "$PANEL_SRC/"
    fi
  elif [[ ! -f "$PANEL_SRC/panel/app/main.py" ]]; then
    missing_files panel
  fi
}

write_panel_unit() {
  cat >/etc/systemd/system/mtpanel.service <<EOF
[Unit]
Description=MTPanel (Telegram proxy panel)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$PANEL_DIR/panel.env
WorkingDirectory=$PANEL_SRC/panel
ExecStart=$PANEL_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port \${MTP_PORT} --ssl-certfile $PANEL_DIR/certs/panel.crt --ssl-keyfile $PANEL_DIR/certs/panel.key --no-server-header --log-level warning --timeout-graceful-shutdown 3
Restart=always
RestartSec=2
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
}

install_panel() {
  base_pkgs
  apt-get install -y -qq python3-venv >/dev/null || die "Could not install python3-venv"
  # certbot lets the panel get a real HTTPS certificate later, from the web settings
  apt-get install -y -qq certbot >/dev/null 2>&1 || warn "certbot could not be installed (only needed for a domain certificate)"
  copy_panel_src
  mkdir -p "$PANEL_DIR/data" "$PANEL_DIR/certs"

  local fresh=0 user="" pass="" ip="" port="" path=""
  if ! panel_installed; then
    fresh=1
    echo
    user=$(ask "Admin username" "admin")
    [[ $user =~ ^[A-Za-z0-9_.@-]{1,32}$ ]] || die "Username may only contain English letters, digits and _ . @ -"
    pass=$(ask "Admin password" "$(rand_str 14)")
    (( ${#pass} >= 8 )) || die "Password must be at least 8 characters"
    case "$pass" in
      *[[:space:]]* | *\"* | *\'* | *\\* | *\$* | *\`*) die "Password may not contain spaces, quotes, \\, \$ or backticks";;
    esac

    ip=$(public_ip)
    port=$(first_free_port 2053 2083 2087 2096 8880 8443) || die "No free port for the panel"
    path=$(rand_str 10)
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PANEL_DIR/certs/panel.key" -out "$PANEL_DIR/certs/panel.crt" \
      -days 3650 -subj "/CN=$ip" 2>/dev/null
    chmod 600 "$PANEL_DIR/certs/panel.key"
    cat >"$PANEL_DIR/panel.env" <<EOF
MTP_PORT=$port
MTP_PANEL_PATH=$path
MTP_HOST=$ip
MTP_PUBLIC_IP=$ip
MTP_ADMIN_USER=$user
MTP_ADMIN_PASS=$pass
MTP_ADMIN_FORCE=1
MTP_DATA_DIR=$PANEL_DIR/data
MTP_CERT_DIR=$PANEL_DIR/certs
MTP_ENV_FILE=$PANEL_DIR/panel.env
MTP_SYNC_INTERVAL=30
MTP_COOKIE_SECURE=1
EOF
    chmod 600 "$PANEL_DIR/panel.env"
    open_port "$port"
  fi

  say "Installing Python packages..."
  [[ -x "$PANEL_DIR/venv/bin/python" ]] || python3 -m venv "$PANEL_DIR/venv"
  "$PANEL_DIR/venv/bin/pip" install -q --upgrade pip
  "$PANEL_DIR/venv/bin/pip" install -q -r "$PANEL_SRC/panel/requirements.txt"

  write_panel_unit
  systemctl daemon-reload
  systemctl enable mtpanel >/dev/null 2>&1
  systemctl restart mtpanel || true
  sleep 4
  systemctl is-active --quiet mtpanel || die "Panel failed to start: journalctl -u mtpanel -n 50 --no-pager"
  install_mgmt_cmd

  echo
  if (( fresh )); then
    echo "${c_b}✅ Panel is up${c_0}"
    echo "Address:   ${c_g}https://$ip:$port/$path/${c_0}"
    echo "Username:  $user"
    echo "Password:  $pass"
    echo
    echo "Open the address and log in; the setup wizard will guide you through the rest."
    echo "(Browser warning about the certificate is normal: Advanced > Proceed)"
  else
    echo "${c_b}✅ Panel updated${c_0}"
    panel_url_line
  fi
  echo "Management menu (any time): mtpanel"
}

panel_url_line() {
  # shellcheck disable=SC1091
  source "$PANEL_DIR/panel.env"
  echo "Address: ${c_g}https://$MTP_HOST:$MTP_PORT/$MTP_PANEL_PATH/${c_0}"
}

panel_address() {
  panel_installed || die "Panel is not installed on this server"
  panel_url_line
  # shellcheck disable=SC1091
  source "$PANEL_DIR/panel.env"
  local np
  np=$(ask "New panel port (Enter = keep)" "$MTP_PORT")
  [[ $np == "$MTP_PORT" ]] && return 0
  [[ $np =~ ^[0-9]+$ ]] && (( np >= 1 && np <= 65535 )) || die "Invalid port"
  port_busy "$np" && die "Port $np is already in use"
  sed -i "s/^MTP_PORT=.*/MTP_PORT=$np/" "$PANEL_DIR/panel.env"
  open_port "$np"
  systemctl restart mtpanel || true
  sleep 3
  panel_url_line
}

uninstall_panel() {
  [[ "$(ask "Uninstall the panel? (y/N)" "n")" =~ ^[Yy] ]] || { echo "Cancelled"; return 0; }
  systemctl disable --now mtpanel 2>/dev/null || true
  rm -f /etc/systemd/system/mtpanel.service
  systemctl daemon-reload
  mv "$PANEL_DIR/panel.env" "$PANEL_DIR/panel.env.removed" 2>/dev/null || true
  remove_mgmt_cmd_if_unused
  warn "Panel removed. Database kept in $PANEL_DIR/data"
}

reset_admin() {
  panel_installed || die "Panel is not installed on this server"
  local pass
  pass=$(ask "New admin password" "$(rand_str 14)")
  (( ${#pass} >= 8 )) || die "Password must be at least 8 characters"
  (cd "$PANEL_SRC/panel" && set -a && . "$PANEL_DIR/panel.env" && set +a && MTP_NEW_PASS="$pass" "$PANEL_DIR/venv/bin/python" -c '
import os
from app import db, auth
db.init()
with db.tx() as c:
    c.execute("UPDATE admins SET pw_hash=? WHERE id=(SELECT MIN(id) FROM admins)", (auth.hash_password(os.environ["MTP_NEW_PASS"]),))
row = db.q1("SELECT username FROM admins ORDER BY id LIMIT 1")
print("Username:", row["username"] if row else "-")
')
  say "Password: $pass"
}

# ============================================================== MENU
menu() {
  local labels=() actions=() i choice
  if panel_installed; then labels+=("Update main panel")
  else labels+=("Install main panel"); fi
  actions+=(install_panel)
  if node_installed; then labels+=("Update node")
  else labels+=("Install node (proxy)"); fi
  actions+=(install_node)
  if panel_installed; then
    labels+=("Panel address / port"); actions+=(panel_address)
    labels+=("Reset admin password"); actions+=(reset_admin)
  fi
  if node_installed; then labels+=("Node connection code"); actions+=(print_conn_code); fi
  if panel_installed; then labels+=("Uninstall panel"); actions+=(uninstall_panel); fi
  if node_installed; then labels+=("Uninstall node"); actions+=(uninstall_node); fi

  echo
  echo "${c_b}MTPanel${c_0}"
  for i in "${!labels[@]}"; do printf ' %d) %s\n' $((i + 1)) "${labels[$i]}"; done
  choice=$(ask "Choose" "")
  [[ $choice =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#actions[@]} )) || die "Invalid choice"
  "${actions[$((choice - 1))]}"
}

case "${1:-}" in
  panel) install_panel;;
  node) install_node;;
  node-code) print_conn_code;;
  panel-address) panel_address;;
  reset-admin) reset_admin;;
  uninstall-panel) uninstall_panel;;
  uninstall-node) uninstall_node;;
  *) menu;;
esac
