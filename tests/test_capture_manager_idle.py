import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from capture_manager import CaptureManager


def make(tmp_path, idle=600.0):
    m = CaptureManager(tmp_path, 8.0, {}, idle_stop_s=idle)
    m.enabled = True
    m.dir.mkdir(parents=True, exist_ok=True)      # CaptureManager.start() does this in the app
    return m


def test_acquisition_switches_off_when_no_page_has_been_open(tmp_path):
    m = make(tmp_path, idle=60)
    now = time.monotonic()
    assert not m._stop_if_abandoned(now + 30) and m.enabled          # somebody was here 30 s ago
    assert m._stop_if_abandoned(now + 61) and not m.enabled
    assert m.auto_stopped_at is not None
    assert m.acquisition_status()["auto_stopped_at"] == m.auto_stopped_at


def test_any_signed_in_request_keeps_it_alive(tmp_path):
    m = make(tmp_path, idle=60)
    m.last_seen = time.monotonic() - 50
    m.touch()
    assert not m._stop_if_abandoned(time.monotonic() + 30) and m.enabled


def test_zero_disables_the_auto_stop_and_enabling_again_clears_the_notice(tmp_path):
    m = make(tmp_path, idle=0)
    m.last_seen = time.monotonic() - 10 ** 6
    assert not m._stop_if_abandoned(time.monotonic()) and m.enabled
    m2 = make(tmp_path, idle=60)
    m2._stop_if_abandoned(time.monotonic() + 100)
    m2.set_enabled(True)
    assert m2.enabled and m2.auto_stopped_at is None


def test_the_control_file_records_the_switch_off(tmp_path):
    import json
    m = make(tmp_path, idle=60)
    m._stop_if_abandoned(time.monotonic() + 100)
    assert json.loads(m.control_path.read_text())["enabled"] is False     # so a restart does not resume it
