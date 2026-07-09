#!/usr/bin/env bash
# Bootstrap the UC External Integration Installer as a background systemd service.
set -euo pipefail

PREFIX="${PREFIX:-/opt/uc-external-integration-installer}"
SERVICE="uc-external-integration-installer"
REPO_URL="${REPO_URL:-https://github.com/jstnjx/uc-external-integration-installer}"
BRANCH="${BRANCH:-main}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking dependencies"
for dep in python3 docker git; do
  need "$dep" || { echo "Missing: $dep"; exit 1; }
done
need python3 && python3 -c 'import venv' 2>/dev/null || {
  echo "python3-venv is required (Debian/Ubuntu: sudo apt install python3-venv)"; exit 1;
}

if [ "${LOCAL_INSTALL:-0}" = "1" ]; then
  echo "==> Installing local files to $PREFIX (requires sudo)"
  sudo mkdir -p "$PREFIX"
  sudo cp -r "$SRC_DIR/uc_installer.py" "$SRC_DIR/static" "$SRC_DIR/requirements.txt" "$PREFIX/"
  # Attach the git remote so the built-in updater can pull later.
  sudo git -C "$PREFIX" init -q 2>/dev/null || true
  sudo git -C "$PREFIX" remote add origin "$REPO_URL" 2>/dev/null \
    || sudo git -C "$PREFIX" remote set-url origin "$REPO_URL" || true
else
  echo "==> Fetching $REPO_URL ($BRANCH) into $PREFIX (requires sudo)"
  if [ -d "$PREFIX/.git" ]; then
    sudo git -C "$PREFIX" remote set-url origin "$REPO_URL"
    sudo git -C "$PREFIX" fetch --depth 1 origin "$BRANCH"
    sudo git -C "$PREFIX" reset --hard FETCH_HEAD
  else
    sudo rm -rf "$PREFIX"
    sudo git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PREFIX"
  fi
fi

echo "==> Creating virtualenv"
sudo python3 -m venv "$PREFIX/venv"
sudo "$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
sudo "$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"

echo "==> Installing systemd unit"
sudo cp "$SRC_DIR/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
sudo mkdir -p /var/lib/uc-external-integration-installer
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "==> Done. UC External Integration Installer is running."
echo "    Open:   http://${IP:-localhost}:8900"
echo "    Logs:   sudo journalctl -u $SERVICE -f"
echo "    Config: sudo systemctl edit $SERVICE   (set UC_INSTALLER_TOKEN etc.)"
echo "    Update: use the version button in the web UI, or re-run this script."
echo
echo "SECURITY: no token is set by default. Anyone who can reach port 8900 can"
echo "control Docker. Set UC_INSTALLER_TOKEN in the unit if this host is shared."
