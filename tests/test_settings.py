"""settings.py: model discovery, validation, data-dir checks, and that the choice reaches the service argv."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import settings as st
from capture_manager import CaptureManager


def make_models(tmp_path):
    m = tmp_path / "models"
    m.mkdir()
    (m / "yolo-a.pt").write_bytes(b"x" * 10)
    (m / "rf.pth").write_bytes(b"x")
    (m / "rf.yaml").write_text("label: Big one\nvariant: medium\n")
    (m / "notes.txt").write_text("ignored")
    (m / "sub").mkdir()
    return m


def test_discovery_types_variants_and_sidecar(tmp_path):
    found = {e["file"]: e for e in st.discover_models(make_models(tmp_path))}
    assert set(found) == {"yolo-a.pt", "rf.pth"}
    assert found["yolo-a.pt"]["type"] == "yolo" and found["yolo-a.pt"]["variant"] is None
    assert found["rf.pth"]["type"] == "rfdetr" and found["rf.pth"]["variant"] == "medium"
    assert found["rf.pth"]["label"] == "Big one"


def test_discovery_survives_missing_folder_and_bad_sidecar(tmp_path):
    assert st.discover_models(tmp_path / "nope") == []
    m = make_models(tmp_path)
    (m / "rf.yaml").write_text(": : not yaml [")
    rf = next(e for e in st.discover_models(m) if e["file"] == "rf.pth")
    assert rf["variant"] == "nano"


def test_store_defaults_roundtrip_and_validation(tmp_path):
    m = make_models(tmp_path)
    store = st.SettingsStore(tmp_path / "settings.yaml", m)
    assert not store.exists and store.get() == st.DEFAULTS
    clean, errors = store.validate({"model": "rf.pth", "setup_done": True})
    assert not errors
    store.update(clean)
    again = st.SettingsStore(tmp_path / "settings.yaml", m)
    assert again.exists and again.get()["model"] == "rf.pth" and "device" not in again.get()


@pytest.mark.parametrize("payload,field", [
    ({"model": "../etc/passwd"}, "model"),
    ({"model": "missing.pt"}, "model"),
    ({"model": "sub/yolo-a.pt"}, "model"),
    ({"data_dir": "relative/path"}, "data_dir"),
    ({"data_dir": "/definitely/not/here"}, "data_dir"),
    ({"setup_done": "yes"}, "setup_done"),
])
def test_validation_rejects(tmp_path, payload, field):
    store = st.SettingsStore(tmp_path / "s.yaml", make_models(tmp_path))
    clean, errors = store.validate(payload)
    assert field in errors and field not in clean


def test_clearing_model_and_data_dir(tmp_path):
    store = st.SettingsStore(tmp_path / "s.yaml", make_models(tmp_path))
    clean, errors = store.validate({"model": "", "data_dir": None})
    assert not errors and clean == {"model": None, "data_dir": None}


def test_check_data_dir_flags_wrong_case_site(tmp_path):
    (tmp_path / "Corbu").mkdir()
    (tmp_path / "babadag").mkdir()
    res = st.check_data_dir(str(tmp_path), ["corbu", "babadag", "newsite"])
    states = {s["name"]: s["state"] for s in res["sites"]}
    assert res["ok"] and states == {"corbu": "wrong_case", "babadag": "present", "newsite": "will_create"}
    assert any("Corbu" in w for w in res["warnings"])


def test_check_data_dir_errors(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert "does not exist" in st.check_data_dir(str(tmp_path / "gone"))["error"]
    assert "not a folder" in st.check_data_dir(str(f))["error"]
    assert not st.check_data_dir("")["ok"]


def test_device_is_not_a_setting_and_a_legacy_key_is_dropped(tmp_path):
    m = make_models(tmp_path)
    (tmp_path / "settings.yaml").write_text("device: cpu\nmodel: rf.pth\nsetup_done: true\n")
    store = st.SettingsStore(tmp_path / "settings.yaml", m)
    assert "device" not in store.get() and store.get()["model"] == "rf.pth"
    clean, errors = store.validate({"device": "cpu"})
    assert clean == {} and not errors                    # unknown keys are ignored, never applied
    store.update({})
    assert "device" not in (tmp_path / "settings.yaml").read_text()


def test_device_flag():
    assert [st.device_flag(d) for d in ("auto", "cpu", "gpu", "junk")] == ["auto", "cpu", "cuda", "auto"]


def test_build_detector_refuses_cuda_without_cuda(monkeypatch):
    import batch_detector
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="GPU requested"):
        batch_detector.build_detector("x.pt", device="cuda")
    with pytest.raises(ValueError):
        batch_detector.build_detector("x.pt", device="tpu")


def test_manager_argv_carries_model_and_device(tmp_path):
    class Site:
        name, config_path, model_bucket, sites_root = "s", "c.yaml", "live", None
    mgr = CaptureManager(tmp_path, 10.0, {"s": "config-weights.pt"})
    mgr.register(Site(), type("Cam", (), {"name": "c"})())
    argv = mgr._argv()
    assert argv[argv.index("--weights") + 1] == "config-weights.pt" and "--model-type" not in argv
    assert argv[argv.index("--device") + 1] == "auto"
    mgr.model_choice = {"weights": "/m/rf.pth", "type": "rfdetr", "variant": "small"}
    mgr.device = "cpu"
    argv = mgr._argv()
    assert argv[argv.index("--weights") + 1] == "/m/rf.pth"
    assert argv[argv.index("--model-type") + 1] == "rfdetr"
    assert argv[argv.index("--rfdetr-variant") + 1] == "small"
    assert argv[argv.index("--device") + 1] == "cpu"


def test_reconfigure_is_noop_when_unchanged_and_never_starts_an_idle_service(tmp_path):
    mgr = CaptureManager(tmp_path, 10.0, {})
    assert mgr.reconfigure(None, "auto") is None
    assert mgr.reconfigure({"weights": "w", "type": "yolo", "variant": None}, "cpu") is None
    assert mgr.process is None and mgr.device == "cpu"


def test_windows_treats_site_case_as_the_same_folder(tmp_path):
    (tmp_path / "Corbu").mkdir()
    res = st.check_data_dir(str(tmp_path), ["corbu"], case_insensitive=True)
    assert res["sites"] == [{"name": "corbu", "state": "present"}] and not res["warnings"]
