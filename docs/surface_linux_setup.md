# Surface Laptop 2 — Ubuntu Linux Setup Guide

## Overview

Replace Windows on the Surface Laptop 2 with Ubuntu 24.04 LTS to use it as a
dedicated nesting worker. This eliminates all the PS remoting / WSL2 NAT issues
and gives us reliable SSH access from the gaming PC.

**End state:** `ssh surface` from gaming PC WSL2 → instant shell on Surface.

## Prerequisites

- USB flash drive (8GB+)
- USB-A hub or adapter (Surface Laptop 2 has 1 USB-A port — you'll need it for
  the USB drive during install, then for the ethernet adapter after)
- Ethernet adapter (USB-A or USB-C)
- Ethernet cable (already connected between gaming PC and Surface)

---

## Part 1: Create Bootable USB (on Gaming PC)

### Option A: From WSL2

```bash
# Download Ubuntu 24.04.1 LTS
cd /mnt/c/temp
wget https://releases.ubuntu.com/24.04/ubuntu-24.04.1-desktop-amd64.iso

# Find your USB drive (plug it in first)
# In Windows PowerShell: Get-Disk | Format-Table
# Note the disk number (e.g., Disk 2)

# Use Rufus (easier) — download from https://rufus.ie
# Or use dd from WSL2 (replace sdX with your USB device):
# sudo dd if=ubuntu-24.04.1-desktop-amd64.iso of=/dev/sdX bs=4M status=progress
```

### Option B: Use Rufus (Recommended)

1. Download Rufus: https://rufus.ie/en/
2. Download Ubuntu 24.04 ISO: https://ubuntu.com/download/desktop
3. Open Rufus → select USB drive → select ISO → Start
4. Use GPT partition scheme, UEFI target

---

## Part 2: Install Ubuntu on Surface

### BIOS/UEFI Settings

1. **Shut down** the Surface completely
2. Hold **Volume Up** + press **Power** to enter UEFI
3. In UEFI settings:
   - **Boot Configuration** → Enable USB boot, move it to top priority
   - **Secure Boot** → Disable (required for linux-surface kernel)
4. Save and exit

### Boot from USB

1. Plug the bootable USB into the Surface
2. Power on — it should boot to Ubuntu installer
3. If not, hold **Volume Down** + press **Power** for one-time boot menu

### Install Ubuntu

1. Select **Install Ubuntu**
2. Connect to WiFi if available (for updates during install), or skip
3. Choose **Erase disk and install Ubuntu** (wipes Windows entirely)
4. Set up user account:
   - Name: `nestworker`
   - Computer name: `surface`
   - Password: choose a password (you'll use this for SSH)
5. Complete installation, reboot, remove USB

---

## Part 3: Post-Install Setup

After Ubuntu boots, open a terminal and run the setup script.

### Get the setup script onto the Surface

**Option A: Via USB drive**
Copy `surface_setup.sh` to the USB drive from gaming PC, then mount on Surface.

**Option B: Via WiFi (if connected)**
```bash
# On gaming PC, serve the file:
cd /home/sarv/projects/MarkerMind/scripts
python3 -m http.server 8080 &

# On Surface (if WiFi connected and on same network):
wget http://<gaming-pc-wifi-ip>:8080/surface_setup.sh
```

**Option C: Type it manually**
The script is short enough to type. See `scripts/surface_setup.sh`.

### Run the setup script

```bash
chmod +x surface_setup.sh
sudo ./surface_setup.sh
```

This script will:
1. Install linux-surface kernel (for keyboard/touchpad support)
2. Configure static IP (192.168.50.2) on ethernet
3. Install and enable SSH server
4. Install Python 3.12 + pip
5. Create a Python venv with spyrrow 0.8.1
6. Copy nesting worker files into place

---

## Part 4: Configure Gaming PC

After the Surface is set up and rebooted, run this on the gaming PC (WSL2):

### 1. Add SSH config

```bash
cat >> ~/.ssh/config << 'EOF'

Host surface
    HostName 192.168.50.2
    User nestworker
    IdentityFile ~/.ssh/id_surface
    StrictHostKeyChecking no
    ConnectTimeout 5
EOF
```

### 2. Set up SSH key (passwordless login)

```bash
# Generate key if you don't have one
ssh-keygen -t ed25519 -f ~/.ssh/id_surface -N ""

# Copy to Surface (will ask for password once)
ssh-copy-id -i ~/.ssh/id_surface nestworker@192.168.50.2
```

### 3. Test connection

```bash
ssh surface "echo 'Connected!' && python3 --version && python3 -c 'import spyrrow; print(f\"spyrrow {spyrrow.__version__}\")'"
```

Expected output:
```
Connected!
Python 3.12.x
spyrrow 0.8.1
```

---

## Part 5: Transfer Job Files

```bash
# Copy worker script and job files to Surface
scp /mnt/c/temp/surface_nesting_worker.py surface:~/
scp /mnt/c/temp/surface_jobs_480s.json surface:~/
scp /mnt/c/temp/surface_jobs_600s.json surface:~/
```

---

## Part 6: Run Nesting Jobs

### Quick test (60s)

```bash
ssh surface "source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py ~/test_60s.json ~/results_test.json"
```

### Long jobs (use tmux for detached execution)

```bash
# Start a tmux session on Surface
ssh surface "tmux new-session -d -s nesting 'source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py ~/surface_jobs_480s.json ~/results_480s.json'"

# Check progress
ssh surface "tmux capture-pane -t nesting -p | tail -5"

# Or check the results file
ssh surface "cat ~/results_480s.json 2>/dev/null | python3 -m json.tool | tail -20"

# Retrieve results when done
scp surface:~/results_480s.json /mnt/c/temp/
```

---

## Troubleshooting

### Surface keyboard/touchpad not working after install
```bash
# The linux-surface kernel should fix this
# If setup script didn't run yet, install manually:
wget -qO - https://raw.githubusercontent.com/linux-surface/linux-surface/master/pkg/keys/surface.asc | sudo gpg --dearmor -o /etc/apt/keyrings/surface.gpg
echo "deb [signed-by=/etc/apt/keyrings/surface.gpg] https://pkg.surfacelinux.com/debian release main" | sudo tee /etc/apt/sources.list.d/linux-surface.list
sudo apt update && sudo apt install -y linux-surface linux-headers-surface
sudo reboot
```

### Can't SSH from gaming PC
```bash
# Check Surface has correct IP
ssh surface "ip addr show"

# Check ethernet link is up
ping 192.168.50.2

# Check SSH is running on Surface
ssh surface "sudo systemctl status ssh"
```

### Ethernet adapter not recognized
Most USB ethernet adapters work out of the box. If not:
```bash
# On Surface, check dmesg for the adapter
dmesg | grep -i eth
lsusb
```

---

## Network Diagram

```
Gaming PC (WSL2)                    Surface Laptop 2 (Ubuntu)
┌─────────────────┐                ┌─────────────────┐
│  192.168.50.1    │◄──ethernet──►│  192.168.50.2    │
│  (Windows host)  │   1Gbps      │  (native Linux)  │
│                  │              │                  │
│  WSL2 bridges    │              │  SSH server      │
│  to host network │              │  Python 3.12     │
│                  │              │  spyrrow 0.8.1   │
└─────────────────┘                └─────────────────┘
     ssh surface ─────────────────► port 22
     scp files   ─────────────────► ~/
```
