#!/bin/sh
# Copy the integration to Home Assistant over SSH (Advanced SSH app, user with sudo).
# Usage: tools/deploy.sh [user@host]
set -e
TARGET="${1:-marcin@192.168.0.2}"
cd "$(dirname "$0")/.."
tar -C custom_components --exclude __pycache__ -czf - dahua_intercom | \
  ssh -o BatchMode=yes "$TARGET" 'sudo -n tar -C /config/custom_components -xzf - && sudo -n find /config/custom_components/dahua_intercom -type f | wc -l'
