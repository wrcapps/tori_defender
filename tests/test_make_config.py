"""The install scripts' config writer: names, validation, escaping, permissions."""
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pytest
import yaml

import make_config as mc
from camera_config import build_cameras


def test_hosts_get_names_and_clashes_are_numbered():
    cams = mc.parse_hosts("192.168.88.41, 192.168.77.41;nvr.local  10.0.0.5=roof")
    assert [c["name"] for c in cams] == ["cam-41", "cam-41-2", "cam3", "roof"]


@pytest.mark.parametrize("bad", ["not a host!", "a/b", "-x", "10.0.0.1=a/b"])
def test_bad_hosts_are_refused(bad):
    with pytest.raises(ValueError):
        mc.parse_hosts(bad)


def test_review_only_config_when_no_cameras():
    assert mc.build("Corbu", "admin", "x", "") == {"site": "corbu"}


def test_site_is_validated():
    with pytest.raises(ValueError):
        mc.build("../etc", "admin", "x", "")


def test_written_file_loads_in_the_app_and_keeps_tricky_passwords(tmp_path):
    out = tmp_path / "config.yaml"
    pw = "p@ss: 'w\"rd #1\\"
    env = dict(os.environ, BIRD_CAM_PASSWORD=pw)
    old, os.environ["BIRD_CAM_PASSWORD"] = os.environ.get("BIRD_CAM_PASSWORD"), pw
    try:
        sys.argv = ["x", "--site", "corbu", "--user", "adm in", "--hosts", "192.168.1.5,192.168.1.6", "--out", str(out)]
        assert mc.main() == 0
    finally:
        if old is None:
            os.environ.pop("BIRD_CAM_PASSWORD")
    cfg = yaml.safe_load(out.read_text())
    cams = build_cameras(cfg)                           # the real loader accepts it
    assert [c.name for c in cams] == ["cam-5", "cam-6"]
    assert cfg["defaults"]["password"] == pw and cfg["defaults"]["username"] == "adm in"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
