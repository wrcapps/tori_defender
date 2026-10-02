#!/usr/bin/env python3
"""Build the desktop app for the OS this runs on:  python desktop/build.py [--light]

PyInstaller cannot cross-compile: run this on Windows to get BirdReview.exe.
Needs:  pip install -r requirements.txt -r desktop/requirements.txt   and node/npm on PATH.
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--light", action="store_true", help="omit the detector stack (Review + Live UI only)")
ap.add_argument("--skip-client", action="store_true", help="reuse the existing client/dist")
ap.add_argument("--node", default=shutil.which("node"), help="node binary (default: from PATH)")
a = ap.parse_args()

if not a.skip_client:
    npm = shutil.which("npm")
    if npm:
        subprocess.check_call([npm, "install"], cwd=ROOT / "client", shell=os.name == "nt")
        subprocess.check_call([npm, "run", "build"], cwd=ROOT / "client", shell=os.name == "nt")
    elif a.node:
        subprocess.check_call([a.node, "node_modules/vite/bin/vite.js", "build"], cwd=ROOT / "client")
    else:
        sys.exit("need npm (or --node with client/node_modules installed) to build the client")

env = dict(os.environ, BIRD_LIGHT="1" if a.light else "0")
subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                       "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                       str(ROOT / "desktop" / "bird-review.spec")], env=env)
print("built:", ROOT / "dist" / "BirdReview")
