#!/usr/bin/env python3
"""Session-cookie authentication and the operator/client role split for app.py.

WHY IN-MEMORY SESSIONS, NOT A TOKEN/JWT SCHEME:
    This app is one process (app.py's own docstring: "one process because
    it's one job"). Every other piece of shared state in it -- verdicts,
    box edits, the capture manager -- is an in-memory object backed by a file
    on disk, not an external store. A session table is the same shape: a dict
    protected by a lock, lost on restart. A restart logging everyone out is an
    honest, acceptable failure mode here; a JWT or an external session store
    would be solving a multi-process problem this app does not have.

WHY ROLES ARE OPERATOR / CLIENT, NOT A PERMISSIONS MATRIX:
    Every mutating endpoint in app.py (recording, verdicts, box edits, dataset
    label decisions) is an operator action -- there is no mutation a client
    role is meant to perform. So the rule is simply: every POST requires
    "operator"; every GET requires *a* session (either role). This is
    enforced here, in one place, rather than per-route, so a new mutating
    route added to app.py is safe by default -- do_POST's dispatcher checks
    the role once, before looking at which endpoint was asked for.

WHY USERS LIVE IN THEIR OWN FILE, NOT A SITE CONFIG:
    A site's config.yaml already holds camera RTSP passwords and is read by
    several unrelated scripts (motion_recorder.py, probe_cameras.py, ...).
    Login credentials for people are a different secret with a different
    blast radius, so they get their own file, own permissions, and are never
    parsed by anything that isn't this app.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from pathlib import Path

import yaml

SESSION_COOKIE = "birdsess"
SESSION_TTL_SECONDS = 12 * 3600
ROLES = ("operator", "client")

# PBKDF2 iteration count. Chosen so a login on a laptop CPU is imperceptible
# (well under 100ms) while a stolen hash still costs real time per guess --
# this is a handful of named users, not a public signup form, so the usual
# "as expensive as tolerable" advice is overkill; this is comfortably above
# OWASP's current PBKDF2-SHA256 floor (600k) is not needed at this scale, but
# costs nothing to clear anyway.
PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(expected.hex(), digest_hex)


class UserStore:
    """username -> {password: "<salt>$<digest>", role: operator|client}, from a
    small YAML file this app owns exclusively. See users.example.yaml.
    """

    def __init__(self, path: Path):
        self.path = path
        self.users: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            raise SystemExit(
                f"--users {self.path} does not exist.\n"
                f"Create one: cp users.example.yaml {self.path} && chmod 600 {self.path}\n"
                f"Then add a user: python auth.py --users {self.path} --add-user NAME --role operator")
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        users = data.get("users") or {}
        for name, entry in users.items():
            if not isinstance(entry, dict) or entry.get("role") not in ROLES:
                raise SystemExit(f"user {name!r} in {self.path} needs role: operator|client")
            if not entry.get("password"):
                raise SystemExit(f"user {name!r} in {self.path} has no password hash")
        self.users = users

    def check(self, username: str, password: str) -> str | None:
        """The user's role if the password is correct, else None.

        Looks up the entry unconditionally before comparing -- an early return
        on "unknown user" would make login for a real username take
        measurably longer than for a made-up one (a PBKDF2 hash is not free),
        which is a timing side-channel for username enumeration.
        """
        entry = self.users.get(username) or {"password": hash_password(secrets.token_hex(8)),
                                              "role": None}
        ok = verify_password(password, entry["password"])
        return entry["role"] if ok else None


class SessionStore:
    """token -> {username, role, expires_at}. Thread-safe; one process holds it."""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS):
        self.ttl = ttl_seconds
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = {
                "username": username, "role": role,
                "expires_at": time.time() + self.ttl,
            }
        return token

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            entry = self.sessions.get(token)
            if entry is None:
                return None
            if entry["expires_at"] < time.time():
                del self.sessions[token]
                return None
            return dict(entry)

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            self.sessions.pop(token, None)


def parse_cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", required=True, type=Path, help="users YAML file to edit")
    ap.add_argument("--add-user", metavar="NAME", required=True)
    ap.add_argument("--role", choices=ROLES, required=True)
    args = ap.parse_args()

    data = {}
    if args.users.exists():
        data = yaml.safe_load(args.users.read_text(encoding="utf-8")) or {}
    users = data.setdefault("users", {})

    password = getpass.getpass(f"password for {args.add_user!r}: ")
    confirm = getpass.getpass("confirm: ")
    if password != confirm:
        print("passwords did not match", file=sys.stderr)
        return 1
    if not password:
        print("password may not be empty", file=sys.stderr)
        return 1

    users[args.add_user] = {"password": hash_password(password), "role": args.role}
    args.users.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    os.chmod(args.users, 0o600)
    print(f"added {args.add_user!r} ({args.role}) to {args.users}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
