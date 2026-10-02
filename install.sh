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

# GPU: nothing to choose. With an NVIDIA GPU the app uses it on its own (detection on CUDA, camera
# video decoded by NVDEC); without one it runs on the CPU. Here we only add the optional NVDEC
# piece, which is the one thing that is not in the base requirements.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  echo "NVIDIA GPU found: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  if ! python -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "  WARNING: this PyTorch cannot see the GPU (CPU-only build or driver problem)."
    echo "  Detection will run on the CPU until a CUDA build of PyTorch is installed: https://pytorch.org/get-started/locally/"
  fi
  if command -v ffmpeg >/dev/null 2>&1; then
    pip install -q --no-deps -r requirements-gpu.txt \
      && echo "  NVDEC video decoding installed." \
      || echo "  Could not install PyNvVideoCodec; video will be decoded on the CPU (everything still works)."
  else
    echo "  ffmpeg is not installed (sudo apt install ffmpeg), so NVDEC video decoding is skipped: video is decoded on the CPU."
    echo "  Install ffmpeg, then run:  pip install --no-deps -r requirements-gpu.txt"
  fi
else
  echo "No NVIDIA GPU detected: everything runs on the CPU (works, but slower with many cameras)."
fi

# ---- 4. config.yaml ----------------------------------------------------------
if [ ! -f config.yaml ]; then
  read -rp "Site name (short, lowercase, e.g. corbu or babadag): " SITE
  CAM_HOSTS=""; CAM_USER="admin"; BIRD_CAM_PASSWORD=""
  read -rp "Connect cameras now? [y/N]: " ADD_CAMS
  if [[ "${ADD_CAMS:-n}" =~ ^[Yy] ]]; then
    echo "The cameras' own login (the one you use in their web page), not this app's login."
    read -rp "  camera username [admin]: " CAM_USER; CAM_USER="${CAM_USER:-admin}"
    read -rsp "  camera password: " BIRD_CAM_PASSWORD; echo
    echo "  Camera IP addresses, separated by commas or spaces (add =name to name one, e.g. 192.168.88.41=mast1-a)."
    read -rp "  cameras: " CAM_HOSTS
  fi
  # The writer escapes everything and sets mode 600; the password travels in the environment, not in argv.
  BIRD_CAM_PASSWORD="$BIRD_CAM_PASSWORD" python backend/make_config.py --site "$SITE" --user "$CAM_USER" \
    --hosts "$CAM_HOSTS" --out config.yaml
  unset BIRD_CAM_PASSWORD
  [ -z "$CAM_HOSTS" ] && echo "No cameras added: Live will be empty. See 'Connecting your cameras' in README.md to add them later."
fi

echo
echo "=== done ==="
echo "Start the reviewer with:"
echo "  source .venv/bin/activate"
echo "  ./backend/app.py --config config.yaml --sites-root ${SITES_ROOT}"
echo "then open http://127.0.0.1:8766/ (forward the port if this is a remote machine)."
