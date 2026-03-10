#!/bin/bash
# =============================================================================
# Surface Laptop 2 — Post-Install Setup Script
# Run this after installing Ubuntu 24.04 on the Surface.
# Usage: sudo ./surface_setup.sh
# =============================================================================
set -euo pipefail

ETHERNET_IP="192.168.50.2"
ETHERNET_GATEWAY="192.168.50.1"
ETHERNET_PREFIX="24"
VENV_DIR="/home/nestworker/nester"
WORKER_USER="nestworker"

echo "============================================"
echo "Surface Laptop 2 — Nesting Worker Setup"
echo "============================================"

# --- 1. System updates and essentials ---
echo ""
echo "[1/6] Installing system packages..."
apt-get update
apt-get install -y \
    openssh-server \
    python3 python3-pip python3-venv \
    tmux htop git curl wget \
    net-tools \
    build-essential

# --- 2. SSH server ---
echo ""
echo "[2/6] Configuring SSH server..."
systemctl enable ssh
systemctl start ssh

# Allow password auth (for initial setup, user can switch to key-only later)
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh

echo "  SSH server running on port 22"

# --- 3. Static IP on ethernet ---
echo ""
echo "[3/6] Configuring static IP on ethernet..."

# Find the ethernet interface name (usually enp0s* or eth*)
ETH_IFACE=$(ip -o link show | grep -v lo | grep -v wl | awk -F: '{print $2}' | tr -d ' ' | head -1)

if [ -z "$ETH_IFACE" ]; then
    echo "  WARNING: No ethernet interface found. Plug in the USB ethernet adapter and re-run."
    echo "  Skipping network config — you can set it up manually later."
else
    echo "  Found ethernet interface: $ETH_IFACE"

    # Create netplan config for static IP
    cat > /etc/netplan/01-ethernet-static.yaml << NETPLAN
network:
  version: 2
  ethernets:
    $ETH_IFACE:
      dhcp4: no
      addresses:
        - ${ETHERNET_IP}/${ETHERNET_PREFIX}
      routes:
        - to: 192.168.50.0/24
          via: ${ETHERNET_IP}
          metric: 100
NETPLAN

    # Don't remove existing WiFi config — keep WiFi for internet access
    chmod 600 /etc/netplan/01-ethernet-static.yaml
    netplan apply 2>/dev/null || echo "  netplan apply had warnings (may need reboot)"

    echo "  Static IP configured: $ETHERNET_IP"
fi

# --- 4. Firewall ---
echo ""
echo "[4/6] Configuring firewall..."
if command -v ufw &>/dev/null; then
    ufw allow ssh
    ufw allow from 192.168.50.0/24
    echo "y" | ufw enable 2>/dev/null || true
    echo "  Firewall: SSH and local subnet allowed"
else
    echo "  ufw not installed, skipping firewall config"
fi

# --- 5. Python venv with spyrrow ---
echo ""
echo "[5/6] Setting up Python environment..."

# Create venv as the worker user
sudo -u "$WORKER_USER" python3 -m venv "$VENV_DIR"
sudo -u "$WORKER_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$WORKER_USER" "$VENV_DIR/bin/pip" install spyrrow==0.8.1

# Verify
SPYRROW_VER=$("$VENV_DIR/bin/python3" -c "import spyrrow; print(spyrrow.__version__)" 2>/dev/null || echo "FAILED")
echo "  Python venv: $VENV_DIR"
echo "  spyrrow version: $SPYRROW_VER"

if [ "$SPYRROW_VER" = "FAILED" ]; then
    echo "  WARNING: spyrrow installation failed. You may need to install it manually."
fi

# --- 6. Add venv to user's bashrc ---
echo ""
echo "[6/6] Configuring user environment..."

BASHRC="/home/$WORKER_USER/.bashrc"
if ! grep -q "nester/bin/activate" "$BASHRC" 2>/dev/null; then
    echo "" >> "$BASHRC"
    echo "# Auto-activate nesting venv" >> "$BASHRC"
    echo "source ~/nester/bin/activate" >> "$BASHRC"
    echo "  Added venv activation to .bashrc"
fi

# --- Done ---
echo ""
echo "============================================"
echo "Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Reboot: sudo reboot"
echo "  2. Plug in ethernet adapter"
echo "  3. From gaming PC: ssh nestworker@192.168.50.2"
echo "  4. Copy job files: scp jobs.json nestworker@192.168.50.2:~/"
echo "  5. Run nesting: python3 surface_nesting_worker.py jobs.json results.json"
echo ""
