#!/usr/bin/env bash
# One-command installer for the UC External Integration Installer.
#   curl -fsSL https://raw.githubusercontent.com/jstnjx/uc-external-integration-installer/main/install.sh | sudo bash
#
# Re-run any time to update. Set LOCAL_INSTALL=1 to install from a local checkout.
set -euo pipefail

PREFIX="${PREFIX:-/opt/uc-external-integration-installer}"
SERVICE="uc-external-integration-installer"
REPO_URL="${REPO_URL:-https://github.com/jstnjx/uc-external-integration-installer}"
BRANCH="${BRANCH:-main}"
DATA_DIR="${UC_INSTALLER_DATA:-/var/lib/uc-external-integration-installer}"
PORT="${UC_INSTALLER_PORT:-8900}"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

need() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking dependencies"
MISSING=""
for dep in python3 docker git curl; do
  need "$dep" || MISSING="$MISSING $dep"
done
if [ -n "$MISSING" ]; then
  echo "    Missing required commands:$MISSING"
  echo "    On Debian/Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv git curl"
  echo "    Docker: https://docs.docker.com/engine/install/"
  exit 1
fi
python3 -c 'import venv' 2>/dev/null || {
  echo "    python3-venv is required (Debian/Ubuntu: sudo apt install -y python3-venv)"; exit 1; }

if [ "${LOCAL_INSTALL:-0}" = "1" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo "==> Installing local files from $SRC_DIR to $PREFIX"
  $SUDO mkdir -p "$PREFIX"
  $SUDO cp -r "$SRC_DIR/uc_installer.py" "$SRC_DIR/static" "$SRC_DIR/requirements.txt" "$PREFIX/"
  $SUDO git -C "$PREFIX" init -q 2>/dev/null || true
  $SUDO git -C "$PREFIX" remote add origin "$REPO_URL" 2>/dev/null \
    || $SUDO git -C "$PREFIX" remote set-url origin "$REPO_URL" || true
else
  echo "==> Fetching $REPO_URL ($BRANCH) into $PREFIX"
  if [ -d "$PREFIX/.git" ]; then
    $SUDO git -C "$PREFIX" remote set-url origin "$REPO_URL"
    $SUDO git -C "$PREFIX" fetch --depth 1 origin "$BRANCH"
    $SUDO git -C "$PREFIX" reset --hard FETCH_HEAD
  else
    $SUDO rm -rf "$PREFIX"
    $SUDO git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PREFIX"
  fi
fi

echo "==> Creating virtualenv and installing dependencies"
$SUDO python3 -m venv "$PREFIX/venv"
$SUDO "$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
$SUDO "$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"

echo "==> Installing Nixpacks (universal source builder for any language)"
if need nixpacks; then
  echo "    already installed: $(nixpacks --version 2>/dev/null || echo present)"
else
  curl -fsSL https://nixpacks.com/install.sh | $SUDO bash \
    || echo "    (nixpacks install failed — built-in Node/Python/.NET/Rust/Go builders will still be used)"
fi

echo "==> Installing systemd unit"
$SUDO mkdir -p "$DATA_DIR"
$SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=UC External Integration Installer
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/venv/bin/python $PREFIX/uc_installer.py
Environment=UC_INSTALLER_HOST=0.0.0.0
Environment=UC_INSTALLER_PORT=$PORT
Environment=UC_INSTALLER_DATA=$DATA_DIR
Environment=UC_INSTALLER_UPDATE_REPO=$REPO_URL
Environment=UC_INSTALLER_UPDATE_BRANCH=$BRANCH
Environment=UC_INSTALLER_SERVICE=$SERVICE
# Optional: token auth, alert webhook (also configurable in the UI)
# Environment=UC_INSTALLER_TOKEN=changeme
# Environment=UC_INSTALLER_ALERT_WEBHOOK=https://ntfy.sh/your-topic
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "==> Done. UC External Integration Installer is running."
echo "    Open http://${IP:-localhost}:$PORT and complete the first-time setup in the UI."
echo "    Logs:   ${SUDO:+sudo }journalctl -u $SERVICE -f"
echo "    Update: use the version button in the web UI, or re-run this command."
