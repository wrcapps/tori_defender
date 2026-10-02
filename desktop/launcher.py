#!/usr/bin/env python3
"""Desktop entry point: the same app, in its own window instead of a browser tab.

    python desktop/launcher.py            # window (needs pywebview)
    python desktop/launcher.py --browser  # same server, opened in the default browser

What it does: starts backend/app.py's server in a thread on a free localhost port,
opens a native window on it, and stops the server (and with it the acquisition
service it spawned) when the window closes. The window signs itself in through a
per-run secret token (app.DESKTOP_TOKEN / GET /desktop-login), so there is no password
prompt for the person sitting at the machine (the token lives only in this process).

Where its files live (config.yaml, users.yaml, sites/, logs/): next to the .exe
when frozen, the repo root otherwise; override with --home or BIRD_REVIEW_HOME.

Frozen, this one exe is also the acquisition service: capture_manager spawns it as
`<exe> --service ...` because there is no Python interpreter to run a script with.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BUNDLE / "backend"))


def default_home() -> Path:
    if os.environ.get("BIRD_REVIEW_HOME"):
        return Path(os.environ["BIRD_REVIEW_HOME"])
    return Path(sys.executable).resolve().parent if FROZEN else BUNDLE


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare_home(home: Path) -> Path:
    """Make the first run work: a config to edit, an (empty) users file the server insists on."""
    config = home / "config.yaml"
    if not config.exists():
        example = BUNDLE / "config.example.yaml"
        if example.exists():
            shutil.copy(example, config)
            print(f"first run: wrote {config} -- edit it for your site and cameras", flush=True)
        else:
            raise SystemExit(f"no {config} and no bundled example to start from")
    users = home / "users.yaml"
    if not users.exists():
        users.write_text("users: {}\n", encoding="utf-8")
    for sub in ("sites", "logs", "models"):
        (home / sub).mkdir(exist_ok=True)
    return config


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--service":
        import acquisition_service
        sys.argv = ["acquisition_service"] + sys.argv[2:]
        return acquisition_service.main()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("--config", type=Path, default=None, help="default: <home>/config.yaml")
    ap.add_argument("--browser", action="store_true", help="open the default browser, not a window")
    ap.add_argument("--port", type=int, default=0, help="default: any free port")
    ap.add_argument("--title", default="Bird Monitoring")
    args, passthrough = ap.parse_known_args()

    home = (args.home or default_home()).resolve()
    config = args.config or prepare_home(home)
    port = args.port or free_port()

    import app
    app.DESKTOP_TOKEN = secrets.token_urlsafe(24)
    ready = threading.Event()
    box: dict = {}

    def on_ready(httpd, manager):
        box["httpd"] = httpd
        ready.set()

    # The local working tree is always <home>/sites; the data folder picked in Settings is the NAS archive
    # that finished data is moved to (backend/archive.py).
    settings_path, models_dir = home / "settings.yaml", home / "models"
    sites_root = ["--sites-root", str(home / "sites")]

    def serve():
        try:
            app.main(["--config", str(config), *sites_root,
                      "--settings", str(settings_path), "--models-dir", str(models_dir),
                      "--users", str(home / "users.yaml"), "--capture-logs", str(home / "logs"),
                      "--port", str(port), *passthrough], on_ready=on_ready)
        finally:
            ready.set()

    server = threading.Thread(target=serve, name="server", daemon=True)
    server.start()
    if not ready.wait(60) or "httpd" not in box:
        print("the server did not start (see the messages above)", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/desktop-login?t={app.DESKTOP_TOKEN}"

    try:
        if args.browser:
            raise ImportError("--browser")
        import webview
    except ImportError as why:
        if str(why) != "--browser":
            print("pywebview is not installed -- opening the browser instead", file=sys.stderr)
        webbrowser.open(url)
        print(f"running -- open {url}  (Ctrl-C to stop)", flush=True)
        try:
            server.join()
        except KeyboardInterrupt:
            pass
    else:
        webview.create_window(args.title, url, width=1440, height=900, min_size=(900, 600))
        webview.start()                       # blocks until the window is closed

    box["httpd"].shutdown()                   # app.main() then stops the acquisition service
    server.join(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
