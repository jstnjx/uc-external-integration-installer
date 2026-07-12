#!/usr/bin/env bash
# Completely remove the UC External Integration Installer, its data, and every
# integration container/image it created.
#
#   curl -fsSL https://raw.githubusercontent.com/jstnjx/uc-external-integration-installer/main/uninstall.sh | sudo bash
#
# Options (env):
#   KEEP_DATA=1     keep /var/lib/... (state, config, backups)
#   KEEP_IMAGES=1   keep locally-built uc-local/* images
#   CONFIRM=1       skip the confirmation prompt (required when piped, no TTY)
set -euo pipefail

PREFIX="${PREFIX:-/opt/uc-external-integration-installer}"
SERVICE="${UC_INSTALLER_SERVICE:-uc-external-integration-installer}"
DATA_DIR="${UC_INSTALLER_DATA:-/var/lib/uc-external-integration-installer}"
KEEP_DATA="${KEEP_DATA:-0}"
KEEP_IMAGES="${KEEP_IMAGES:-0}"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "This will PERMANENTLY remove:"
echo "  • systemd service:  $SERVICE"
echo "  • install dir:      $PREFIX"
[ "$KEEP_DATA" = "1" ]   || echo "  • data dir:         $DATA_DIR  (state, config, backups)"
echo "  • all containers labelled uc.installer=managed  (your installed integrations)"
[ "$KEEP_IMAGES" = "1" ] || echo "  • locally-built images (uc-local/*)"
echo

if [ "${CONFIRM:-0}" != "1" ]; then
  if [ -e /dev/tty ]; then
    read -r -p "Continue? [y/N] " ans </dev/tty
    case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 1;; esac
  else
    echo "Refusing to wipe without a confirmation. Re-run with CONFIRM=1 to proceed."
    exit 1
  fi
fi

echo "==> Stopping and disabling the service"
$SUDO systemctl disable --now "$SERVICE" 2>/dev/null || true

if command -v docker >/dev/null 2>&1; then
  echo "==> Removing managed integration containers"
  cids="$(docker ps -aq --filter "label=uc.installer=managed" 2>/dev/null || true)"
  if [ -n "$cids" ]; then $SUDO docker rm -f $cids; else echo "    none found"; fi

  if [ "$KEEP_IMAGES" != "1" ]; then
    echo "==> Removing locally-built images (uc-local/*)"
    imgs="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^uc-local/' || true)"
    if [ -n "$imgs" ]; then $SUDO docker rmi -f $imgs 2>/dev/null || true; else echo "    none found"; fi
  fi
else
  echo "==> docker not found — skipping container/image cleanup"
fi

echo "==> Removing systemd unit"
$SUDO rm -f "/etc/systemd/system/$SERVICE.service"
$SUDO systemctl daemon-reload 2>/dev/null || true

echo "==> Removing install directory"
$SUDO rm -rf "$PREFIX"

if [ "$KEEP_DATA" != "1" ]; then
  echo "==> Removing data directory"
  $SUDO rm -rf "$DATA_DIR"
fi

echo
echo "==> Done. The installer and its integrations have been removed."
echo "    NOTE: driver registrations on your UC remote(s) are NOT removed automatically."
echo "    If any remain, delete them from the remote's web-configurator → Integrations."
[ "$KEEP_IMAGES" = "1" ] || echo "    Pulled GHCR images (if any) can be cleared with:  docker image prune -a"
