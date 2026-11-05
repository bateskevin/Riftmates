
#!/usr/bin/env bash
set -euo pipefail

# ============================================
# RIFTMATES DEPLOYMENT CONFIGURATION
# ============================================

PI_USER="kevinbates"
PI_HOST="192.168.68.122"
PI_PORT="22"

REMOTE_DIR="/opt/riftmates"
SERVICE="riftmates"

# Directory containing this script
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# ============================================
# PRE-FLIGHT CHECKS
# ============================================

echo "======================================"
echo "  RIFTMATES DEPLOYMENT"
echo "======================================"

if [[ ! -f "$LOCAL_DIR/server.py" ]]; then
    echo "ERROR: server.py not found in $LOCAL_DIR"
    exit 1
fi

echo "Source: $LOCAL_DIR"
echo "Target: $PI_USER@$PI_HOST:$REMOTE_DIR"
echo

# ============================================
# 1. CHECK SSH CONNECTION
# ============================================

echo "[1/4] Checking SSH connection..."

ssh -p "$PI_PORT" \
    "$PI_USER@$PI_HOST" \
    "test -d '$REMOTE_DIR' && test -x '$REMOTE_DIR/.venv/bin/python'"

# ============================================
# 2. COPY SOURCE FILES
# ============================================

echo "[2/4] Synchronizing Riftmates files..."

rsync -avz --progress \
    -e "ssh -p $PI_PORT" \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.db' \
    --exclude='*.db-*' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite-*' \
    --exclude='*.sqlite3' \
    --exclude='*.sqlite3-*' \
    --exclude='*.json' \
    --exclude='*.log' \
    --exclude='logs/' \
    --exclude='deploy.sh' \
    "$LOCAL_DIR/" \
    "$PI_USER@$PI_HOST:$REMOTE_DIR/"

# ============================================
# 3. INSTALL DEPENDENCIES
# ============================================

echo "[3/4] Checking Python dependencies..."

ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" \
    "cd '$REMOTE_DIR' && \
     .venv/bin/python -m pip install -r requirements.txt && \
     .venv/bin/python -m compileall -q server.py"

# ============================================
# 4. RESTART SERVICE
# ============================================

echo "[4/4] Restarting Riftmates..."

ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" \
    "sudo systemctl restart '$SERVICE' && \
     sleep 3 && \
     systemctl is-active --quiet '$SERVICE' && \
     systemctl --no-pager status '$SERVICE'"

echo
echo "======================================"
echo "  DEPLOYMENT SUCCESSFUL"
echo "======================================"
