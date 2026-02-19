#!/usr/bin/env bash
set -euo pipefail

# lucid-logbook deploy script for Rocky Linux 10 (Proxmox VM)
# Usage: sudo bash deploy.sh [--repo-url URL]

APP_DIR="/opt/lucid-logbook"
APP_USER="lucid-logbook"
SERVICE_NAME="lucid-logbook"
PYTHON="python3"
REPO_URL="${1:-}"

echo "=== lucid-logbook deployment ==="

# --- System deps ---
echo "[1/6] Installing system dependencies..."
dnf install -y python3 python3-pip git 2>/dev/null || \
    yum install -y python3 python3-pip git

# --- Service user ---
echo "[2/6] Creating service user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /sbin/nologin --home-dir "$APP_DIR" "$APP_USER"
    echo "  Created user: $APP_USER"
else
    echo "  User $APP_USER already exists"
fi

# --- App directory ---
echo "[3/6] Setting up $APP_DIR..."
mkdir -p "$APP_DIR"

if [ -n "$REPO_URL" ]; then
    # Clone or pull from git
    if [ -d "$APP_DIR/.git" ]; then
        cd "$APP_DIR" && git pull
    else
        git clone "$REPO_URL" "$APP_DIR"
    fi
elif [ -f "$(dirname "$0")/../pyproject.toml" ]; then
    # Deploy from local repo (script is in deploy/)
    SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    rsync -a --exclude='.venv' --exclude='*.db' --exclude='.env' \
        "$SCRIPT_DIR/" "$APP_DIR/"
else
    echo "ERROR: No repo URL given and not running from repo. Pass --repo-url or run from the repo."
    exit 1
fi

# --- Virtualenv & install ---
echo "[4/6] Setting up Python venv and installing..."
cd "$APP_DIR"
$PYTHON -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# --- Env file ---
echo "[5/6] Configuring environment..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "  Created .env from .env.example — edit it before starting!"
else
    echo "  .env already exists, skipping"
fi

# Fix ownership
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- systemd ---
echo "[6/6] Installing systemd service..."
cp "$APP_DIR/deploy/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/lucid-logbook/.env"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. sudo systemctl status $SERVICE_NAME"
echo "  4. journalctl -u $SERVICE_NAME -f    (logs)"
echo ""
