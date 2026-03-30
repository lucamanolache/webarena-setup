#!/bin/bash
set -e

echo "Installing dependencies for webarena-setup..."

apt-get update -qq

# podman: container runtime
apt-get install -y -qq podman

# nginx: reverse proxy for hot-swap port routing
apt-get install -y -qq nginx

# curl: used by health checks and reset client
apt-get install -y -qq curl

# python3: reset server
apt-get install -y -qq python3

# Disable default nginx site (port 80 is used by the homepage server)
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
systemctl start nginx

# Allow unprivileged ports from 80+ (for podman containers)
sysctl -w net.ipv4.ip_unprivileged_port_start=80
grep -q 'ip_unprivileged_port_start' /etc/sysctl.conf 2>/dev/null || \
  echo 'net.ipv4.ip_unprivileged_port_start=80' >> /etc/sysctl.conf

echo "All dependencies installed."
