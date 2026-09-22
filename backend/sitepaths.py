#!/usr/bin/env python3
"""Where a run's output goes:  <sites-root>/<site>/<kind>/...  , named by the config.

THE SITE COMES FROM THE CONFIG FILE, NEVER FROM argv:
    The config already decides which cameras a run talks to, so letting it also
    decide where the output lands means the destination cannot disagree with the
    source. A missing `site:` is an error, not a default -- falling back to some
    previous site's name would put a new installation's data in the old one's
    folder, which only shows up much later in a dataset that spans two places.

THE SITES ROOT, HOWEVER, IS A FLAG:
    This app ships as its own directory, so by default it keeps its own
    `sites/` beside it. Point --sites-root at a shared tree (a NAS mount, an
    existing pipeline's directory) to read and write there instead, rather than
    silently growing a second, divergent copy of the same site.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_SITES_ROOT = ROOT / "sites"

# A site name becomes a directory, so it may not wander out of the sites root.
VALID_SITE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def add_sites_root_argument(parser) -> None:
    """Every CLI in this app takes the same --sites-root, described the same way."""
    parser.add_argument(
        "--sites-root", type=Path, default=None,
        help=f"where site data lives (default: {DEFAULT_SITES_ROOT}). Point this at "
             f"a shared tree to work against existing captures instead of starting "
             f"a separate copy.")


def site_of(cfg: dict, source: str | Path = "the config") -> str:
    """The site a config belongs to. Raises if it does not name one."""
    site = (cfg or {}).get("site")
    if not site:
        raise SystemExit(
            f"{source} does not set `site:`.\n"
            f"Add one at the top of the file, e.g.  site: corbu\n"
            f"It decides which <sites-root>/<site>/ directory this run writes to; "
            f"there is deliberately no default, so that one installation cannot "
            f"silently write into another one's results.")
    site = str(site).strip().lower()
    if not VALID_SITE.match(site):
        raise SystemExit(
            f"{source}: site {site!r} is not a usable directory name "
            f"(lowercase letters, digits and dashes only).")
    return site


def site_root(cfg: dict, base: Path | None = None,
              source: str | Path = "the config") -> Path:
    """<sites-root>/<site> for this config, created if absent."""
    root = (base or DEFAULT_SITES_ROOT) / site_of(cfg, source)
    root.mkdir(parents=True, exist_ok=True)
    return root


def site_dir(cfg: dict, *parts: str, base: Path | None = None,
             source: str | Path = "the config") -> Path:
    """<sites-root>/<site>/<parts...>, created if absent."""
    path = site_root(cfg, base, source).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def known_sites(base: Path | None = None) -> list[str]:
    """Sites that already have a directory, for error messages and listings."""
    sites = base or DEFAULT_SITES_ROOT
    if not sites.is_dir():
        return []
    return sorted(p.name for p in sites.iterdir() if p.is_dir())


if __name__ == "__main__":
    for name in known_sites():
        print(name)
