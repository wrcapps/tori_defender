# PyInstaller recipe for the desktop app. Build it with desktop/build.py (or build_windows.bat),
# which builds the client first. BIRD_LIGHT=1 leaves out the detector stack (torch, ultralytics,
# rfdetr): a ~10x smaller exe that runs Review and the Live UI but cannot start capture.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
LIGHT = os.environ.get("BIRD_LIGHT") == "1"

# The service modules are imported by name at runtime (`--service`), so analysis must be told.
hidden = ["acquisition_service", "batch_detector", "segment_feed", "nvr_archive", "tracker",
          "live_capture", "risk", "metrics_log", "dataset_review", "link_watch_read"]
datas = [(str(ROOT / "client" / "dist"), "client/dist"), (str(ROOT / "config.example.yaml"), ".")]
excludes = ["tkinter", "matplotlib", "pytest"]
if LIGHT:
    excludes += ["torch", "torchvision", "ultralytics", "rfdetr", "onnxruntime", "scipy", "pandas"]
else:
    hidden += collect_submodules("ultralytics") + collect_submodules("rfdetr")

a = Analysis([str(ROOT / "desktop" / "launcher.py")], pathex=[str(ROOT / "backend")],
             datas=datas, hiddenimports=hidden, excludes=excludes)
pyz = PYZ(a.pure)
# console=False: no black terminal window behind the app. Messages go to logs/ instead.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BirdReview", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="BirdReview")
