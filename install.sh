#!/bin/bash
# One-time setup for a client machine: mount the shared Tori NAS, install the
# app's Python dependencies, and write a config.yaml pointed at it.
#
# CREDENTIALS ARE NEVER IN THIS FILE. It asks for them interactively (or reads
# them from a credentials file you already have) and writes them ONLY to
# /etc/samba/tori-credentials (root-owned, chmod 600) or, if you cannot use
# sudo, to a local credentials file with the same permissions -- never into
# this script, never into config.yaml, never into shell history (read -s).
set -euo pipefail
cd "$(dirname "$0")"

# Non-secret connection settings can come from nas.env (see nas.example.env);
# credentials are always asked for interactively below, never read from a file.
if [ -f nas.env ]; then
  echo "Using NAS settings from nas.env."
  set -a; source nas.env; set +a
fi

TORI_HOST="${TORI_HOST:-192.168.1.138}"
TORI_SHARE="${TORI_SHARE:-Pasari}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/Tori}"
CRED_FILE="${CRED_FILE:-$HOME/.tori-credentials}"

echo "=== bird-review-app client setup ==="

# ---- 1. Tori (NAS) mount ---------------------------------------------------
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
  echo "$MOUNT_POINT already mounted, skipping."
else
  echo "Mounting //${TORI_HOST}/${TORI_SHARE} at ${MOUNT_POINT}."
  echo "Enter the CIFS credentials given to you for this share (not your own login)."
  read -rp "  username: " TORI_USER
  read -rsp "  password: " TORI_PASS; echo
  read -rp "  domain [TRUENAS]: " TORI_DOMAIN
  TORI_DOMAIN="${TORI_DOMAIN:-TRUENAS}"

  umask 077
  printf 'username=%s\npassword=%s\ndomain=%s\n' \
    "$TORI_USER" "$TORI_PASS" "$TORI_DOMAIN" > "$CRED_FILE"
  unset TORI_PASS

  sudo mkdir -p "$MOUNT_POINT"
  sudo mount -t cifs "//${TORI_HOST}/${TORI_SHARE}" "$MOUNT_POINT" \
    -o "credentials=${CRED_FILE},uid=$(id -u),gid=$(id -g),vers=3.0"
  echo "mounted. Add this to /etc/fstab yourself if you want it to survive a reboot:"
  echo "  //${TORI_HOST}/${TORI_SHARE} ${MOUNT_POINT} cifs credentials=${CRED_FILE},uid=$(id -u),gid=$(id -g),vers=3.0 0 0"
fi

# ---- 2. lowercase site aliases ---------------------------------------------
# sitepaths.py requires lowercase site names; the NAS directories are
# capitalised (Corbu, Babadag). CIFS does not support symlinks written INSIDE
# the share, so the alias is a local dir of symlinks pointing INTO the mount.
SITES_ROOT="$(pwd)/tori_sites"
mkdir -p "$SITES_ROOT"
for pair in "corbu:Corbu" "babadag:Babadag"; do
  lower="${pair%%:*}"; upper="${pair##*:}"
  if [ -d "${MOUNT_POINT}/Dataset/${upper}" ] && [ ! -e "${SITES_ROOT}/${lower}" ]; then
    ln -s "${MOUNT_POINT}/Dataset/${upper}" "${SITES_ROOT}/${lower}"
  fi
done

# ---- 3. Python environment --------------------------------------------------
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

# ---- 4. config.yaml ----------------------------------------------------------
if [ ! -f config.yaml ]; then
  read -rp "Which site will this reviewer look at [corbu/babadag]: " SITE
  cat > config.yaml <<EOF
# Review-only config: no camera list needed to browse already-captured data.
site: ${SITE}
EOF
  chmod 600 config.yaml
fi

echo
echo "=== done ==="
echo "Start the reviewer with:"
echo "  source .venv/bin/activate"
echo "  ./backend/app.py --config config.yaml --sites-root ${SITES_ROOT}"
echo "then open http://127.0.0.1:8766/ (forward the port if this is a remote machine)."
