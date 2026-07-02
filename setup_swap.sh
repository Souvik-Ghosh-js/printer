#!/usr/bin/env bash
# ============================================================
#  Add a swap file to the Lightsail instance as an OOM fallback.
#  Safe + idempotent: does nothing if swap already exists.
#
#  RUN:  bash setup_swap.sh          # auto-sizes swap
#        SWAP_SIZE=4G bash setup_swap.sh   # or force a size
# ============================================================
set -euo pipefail

SWAPFILE="/swapfile"

# --- Decide swap size ---
# Default rule: 2x RAM up to 2G RAM, else = RAM, capped at 4G.
if [ -z "${SWAP_SIZE:-}" ]; then
  MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
  if   [ "$MEM_MB" -le 2048 ]; then SWAP_SIZE="$((MEM_MB*2))M"
  elif [ "$MEM_MB" -le 8192 ]; then SWAP_SIZE="${MEM_MB}M"
  else SWAP_SIZE="4G"
  fi
fi

echo "==> Current memory / swap:"
free -h

# --- Bail out if swap is already active ---
if swapon --show | grep -q .; then
  echo "==> Swap already active — nothing to do:"
  swapon --show
  exit 0
fi

# --- Check free disk (don't fill the 20GB SSD) ---
AVAIL_MB=$(df -m --output=avail / | tail -1 | tr -d ' ')
echo "==> Free disk on /: ${AVAIL_MB}MB, creating ${SWAP_SIZE} swap"

echo "==> [1/5] Allocate ${SWAP_SIZE} at ${SWAPFILE}..."
# fallocate is instant; fall back to dd if the fs doesn't support it.
if ! sudo fallocate -l "$SWAP_SIZE" "$SWAPFILE" 2>/dev/null; then
  echo "    fallocate unsupported, using dd (slower)..."
  # strip unit for dd count in MB
  COUNT=$(numfmt --from=iec "$SWAP_SIZE")
  sudo dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((COUNT/1024/1024)) status=progress
fi

echo "==> [2/5] Permissions (root only)..."
sudo chmod 600 "$SWAPFILE"

echo "==> [3/5] Format as swap..."
sudo mkswap "$SWAPFILE"

echo "==> [4/5] Enable it now..."
sudo swapon "$SWAPFILE"

echo "==> [5/5] Persist across reboots (fstab)..."
if ! grep -q "^${SWAPFILE} " /etc/fstab; then
  echo "${SWAPFILE} none swap sw 0 0" | sudo tee -a /etc/fstab
fi

# Tune: prefer RAM, only swap under real pressure (good for a server).
sudo sysctl -w vm.swappiness=10 >/dev/null
if ! grep -q "^vm.swappiness" /etc/sysctl.conf; then
  echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf >/dev/null
fi

echo "==> Done. New memory / swap:"
free -h
