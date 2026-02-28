#!/bin/bash
# Install YoLocal integration from a dev web server to Home Assistant.
# Set DEV_WEB_SERVER_URL to only scheme + hostname + port.
# 
# Example:
#   DEV_WEB_SERVER_URL="http://your-dev-host:8000"

set -e

DEV_WEB_SERVER_URL="${DEV_WEB_SERVER_URL:-http://higgs:8000}"
REPO_URL="${DEV_WEB_SERVER_URL%/}/yolink-local-ha"
INSTALL_DIR="/config/custom_components/yolocal"
API_DIR="$INSTALL_DIR/api"

echo "Installing YoLocal integration from $REPO_URL..."

# Test connection to server
if ! wget -q --spider "$REPO_URL/custom_components/yolocal/__init__.py"; then
    echo "ERROR: Cannot connect to $REPO_URL"
    echo "Make sure your dev web server is running and DEV_WEB_SERVER_URL is correct."
    exit 1
fi

# Remove existing integration if present
if [ -d "$INSTALL_DIR" ]; then
    echo "Removing existing installation..."
    rm -rf "$INSTALL_DIR"
fi

# Create directories
mkdir -p "$API_DIR"

# Download main files
echo "Downloading main files..."
wget -q --show-progress "$REPO_URL/custom_components/yolocal/__init__.py" -O "$INSTALL_DIR/__init__.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/binary_sensor.py" -O "$INSTALL_DIR/binary_sensor.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/config_flow.py" -O "$INSTALL_DIR/config_flow.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/const.py" -O "$INSTALL_DIR/const.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/coordinator.py" -O "$INSTALL_DIR/coordinator.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/entity.py" -O "$INSTALL_DIR/entity.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/lock.py" -O "$INSTALL_DIR/lock.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/sensor.py" -O "$INSTALL_DIR/sensor.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/siren.py" -O "$INSTALL_DIR/siren.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/switch.py" -O "$INSTALL_DIR/switch.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/manifest.json" -O "$INSTALL_DIR/manifest.json"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/strings.json" -O "$INSTALL_DIR/strings.json"

# Download api files
echo "Downloading api files..."
wget -q --show-progress "$REPO_URL/custom_components/yolocal/api/__init__.py" -O "$API_DIR/__init__.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/api/auth.py" -O "$API_DIR/auth.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/api/client.py" -O "$API_DIR/client.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/api/device.py" -O "$API_DIR/device.py"
wget -q --show-progress "$REPO_URL/custom_components/yolocal/api/mqtt.py" -O "$API_DIR/mqtt.py"

# Clear Python cache
echo "Clearing Python cache..."
find "$INSTALL_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

echo ""
echo "✅ Installation complete!"
echo "Files installed to: $INSTALL_DIR"
echo ""
echo "Restarting Home Assistant Core..."
if command -v ha >/dev/null 2>&1; then
    ha core restart
    echo "✅ Restart command sent: ha core restart"
else
    echo "⚠️ 'ha' CLI not found. Please restart Home Assistant manually."
fi
