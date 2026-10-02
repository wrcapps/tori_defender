#!/usr/bin/env python3
"""Write a starter config.yaml from the install scripts' answers (so nobody hand-edits YAML).

    BIRD_CAM_PASSWORD=... python backend/make_config.py --site corbu --user admin \\
        --hosts "192.168.88.41, 192.168.88.42=mast1-b" --out config.yaml

--hosts is a list separated by commas, spaces or semicolons. Each item is `host` or `host=name`.
A camera without a name is called `cam-<last number of its IP>` (`cam1`, `cam2`... for host names),
and a clash gets a numeric suffix. The camera password is read from the environment, never from
argv, and the file is written with mode 600. Everything else (stream address, scan rates, retention)
keeps its default; see config.example.yaml for what can be added by hand.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

HOST_OK = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")
SITE_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def parse_hosts(text: str) -> list[dict]:
    cameras, used = [], set()
    for item in re.split(r"[,;\s]+", text.strip()):
        if not item:
            continue
        host, _, name = item.partition("=")
        if not HOST_OK.match(host):
            raise ValueError(f"{host!r} is not an IP address or host name")
        if not name:
            tail = host.rsplit(".", 1)[-1]
            name = f"cam-{tail}" if re.fullmatch(r"\d{1,3}", tail) and host.count(".") == 3 else f"cam{len(cameras) + 1}"
        if not NAME_OK.match(name):
            raise ValueError(f"camera name {name!r} may only use letters, digits, dot, dash, underscore")
        base, n = name, 1
        while name in used:
            n += 1
            name = f"{base}-{n}"
        used.add(name)
        cameras.append({"name": name, "host": host})
    return cameras


def build(site: str, user: str, password: str, hosts: str) -> dict:
    site = site.strip().lower()
    if not SITE_OK.match(site):
        raise ValueError("site: lowercase letters, digits and dashes only")
    cfg: dict = {"site": site}
    cameras = parse_hosts(hosts) if hosts else []
    if cameras:
        cfg["defaults"] = {"username": user, "password": password,
                           "record_url": "rtsp://{host}:554/Streaming/Channels/101"}
        cfg["cameras"] = cameras
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--hosts", default="")
    ap.add_argument("--out", type=Path, default=Path("config.yaml"))
    args = ap.parse_args()
    try:
        cfg = build(args.site, args.user, os.environ.get("BIRD_CAM_PASSWORD", ""), args.hosts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    header = ("# Written by the install script. Add cameras, change the stream address or tune scanning by\n"
              "# hand: see config.example.yaml. This file holds the camera passwords: do not share it.\n")
    args.out.write_text(header + yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.chmod(args.out, 0o600)
    print(f"wrote {args.out}: site {cfg['site']}, {len(cfg.get('cameras', []))} camera(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
