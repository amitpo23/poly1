#!/usr/bin/env bash
# One-time VPS bootstrap (Ubuntu 22.04). Run as root.
# Usage:  curl -fsSL <raw>/deploy/vps-bootstrap.sh | sudo bash -s -- <ssh_pubkey>
set -euo pipefail

PUBKEY="${1:-}"
if [[ -z "$PUBKEY" ]]; then
  echo "usage: $0 '<ssh-public-key>'" >&2
  exit 1
fi

echo "[1/8] creating user 'trader' (uid 10001)"
if ! id trader >/dev/null 2>&1; then
  useradd -m -u 10001 -s /bin/bash trader
fi
mkdir -p /home/trader/.ssh
echo "$PUBKEY" >> /home/trader/.ssh/authorized_keys
chown -R trader:trader /home/trader/.ssh
chmod 700 /home/trader/.ssh
chmod 600 /home/trader/.ssh/authorized_keys

echo "[2/8] enabling passwordless sudo for trader (deploy convenience)"
echo "trader ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-trader
chmod 440 /etc/sudoers.d/90-trader

echo "[3/8] hardening sshd: keys only"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart ssh

echo "[4/8] firewall (UFW)"
apt-get update -y
apt-get install -y ufw fail2ban unattended-upgrades chrony rsync sqlite3
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "[5/8] swap (2GB)"
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "[6/8] timezone UTC + chrony"
timedatectl set-timezone UTC
systemctl enable --now chrony

echo "[7/8] Docker via official script"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker trader

echo "[8/8] /srv/poly1 directory"
mkdir -p /srv/poly1
chown -R trader:trader /srv/poly1

echo
echo "DONE. Next:"
echo "  ssh trader@<VPS>"
echo "  git clone <repo> /srv/poly1"
echo "  scp .env trader@<VPS>:/srv/poly1/.env && chmod 600 /srv/poly1/.env"
echo "  cd /srv/poly1 && docker compose build && docker compose up -d"
