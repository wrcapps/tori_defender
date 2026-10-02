#!/usr/bin/env python3
"""Per-installation choices made in the app (Settings page / first-run setup), kept in settings.yaml.

WHAT IS CHOSEN HERE
    model     a file in models/ (or None = whatever config.yaml / --weights says, as before)
    data_dir  where site data lives (the --sites-root); None = <app>/sites

WHY A SECOND FILE, NOT config.yaml
    config.yaml holds camera passwords and is hand-edited per site; it must not be rewritten by a
    web form. settings.yaml holds only these three choices plus `setup_done`, so a missing file means
    "nothing chosen": the app behaves exactly as it did before this module existed.

WHAT APPLIES WHEN
    model           at once: the acquisition service is restarted with the new flag.
    data_dir        next start: every Site object and the service already hold the old root.

models/
    *.pt   -> YOLO (ultralytics)            *.pth -> RF-DETR (rfdetr checkpoint)
    An optional <same-stem>.yaml beside a model sets `label`, `type` and `variant` (RF-DETR size:
    nano|small|medium|base|large -- must match what the checkpoint was finetuned as, nano if unset).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_MODELS_DIR = ROOT / "models"
DEFAULT_SETTINGS_PATH = ROOT / "settings.yaml"

WINDOWS = os.name == "nt"
MODEL_SUFFIXES = {".pt": "yolo", ".pth": "rfdetr"}
RFDETR_VARIANTS = ("nano", "small", "medium", "base", "large")
DEFAULTS = {"model": None, "data_dir": None, "setup_done": False}
# There is deliberately no CPU/GPU choice: the service always runs `--device auto` (GPU when CUDA is
# usable, else CPU). A `device:` key left in an old settings.yaml is ignored and dropped on next save.


# ----------------------------------------------------------------------------- models
def discover_models(models_dir: Path) -> list[dict]:
    """Every model file directly inside models_dir, with its type resolved. Never raises: a missing
    folder is an empty list, a bad sidecar is ignored (the file is still listed with defaults)."""
    if not models_dir.is_dir():
        return []
    found = []
    for path in sorted(models_dir.iterdir(), key=lambda p: p.name.lower()):
        mtype = MODEL_SUFFIXES.get(path.suffix.lower())
        if mtype is None or not path.is_file():
            continue
        meta: dict = {}
        sidecar = path.with_suffix(".yaml")
        if sidecar.is_file():
            try:
                meta = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        mtype = meta.get("type") if meta.get("type") in ("yolo", "rfdetr") else mtype
        variant = meta.get("variant") if meta.get("variant") in RFDETR_VARIANTS else "nano"
        found.append({
            "file": path.name,
            "label": str(meta.get("label") or path.stem),
            "type": mtype,
            "variant": variant if mtype == "rfdetr" else None,
            "size_mb": round(path.stat().st_size / 2**20, 1),
        })
    return found


def resolve_model(file: str | None, models_dir: Path) -> dict | None:
    """The {weights, type, variant, file} a chosen model file stands for, or None if there is no
    choice or the file is gone (the caller then falls back to config.yaml rather than failing)."""
    if not file:
        return None
    for entry in discover_models(models_dir):
        if entry["file"] == file:
            return {"weights": str(models_dir / file), "type": entry["type"],
                    "variant": entry["variant"], "file": file}
    return None


# ---------------------------------------------------------------------------- devices
_PROBE = (
    "import json\n"
    "try:\n"
    "    import torch\n"
    "    cuda = torch.cuda.is_available()\n"
    "    gpus = [{'name': torch.cuda.get_device_name(i),\n"
    "             'memory_mb': round(torch.cuda.get_device_properties(i).total_memory / 2**20)}\n"
    "            for i in range(torch.cuda.device_count())] if cuda else []\n"
    "    out = {'torch': torch.__version__, 'cuda': cuda, 'gpus': gpus}\n"
    "except ImportError:\n"
    "    out = {'torch': None, 'cuda': False, 'gpus': []}\n"
    "print(json.dumps(out))\n")

_device_cache: dict | None = None
_device_lock = threading.Lock()


def detect_devices(refresh: bool = False) -> dict:
    """What this machine can run on: {torch, cuda, gpus[{name, memory_mb}], error?}.

    Probed in a child process, not here: importing torch costs seconds and hundreds of MB, and
    initialising CUDA in the web server would hold a GPU context the acquisition service
    should own. Frozen builds have no interpreter to run a probe with, so they import in-process.
    Cached: the answer cannot change without a driver install."""
    global _device_cache
    with _device_lock:
        if _device_cache is not None and not refresh:
            return _device_cache
        try:
            if getattr(sys, "frozen", False):
                ns: dict = {}
                import contextlib
                import io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    exec(_PROBE, ns)
                raw = buf.getvalue()
            else:
                # No console window flashing up on Windows while torch is imported.
                extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
                raw = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                                     timeout=60, check=True, **extra).stdout
            import json
            result = json.loads(raw.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            result = {"torch": None, "cuda": False, "gpus": [], "error": f"could not probe devices: {exc}"}
        _device_cache = result
        return result


def device_flag(setting: str) -> str:
    """The value acquisition_service.py's --device takes for a settings value (gpu -> cuda)."""
    return {"auto": "auto", "cpu": "cpu", "gpu": "cuda"}.get(setting, "auto")


# --------------------------------------------------------------------------- data dir
def check_data_dir(path: str, site_names: list[str] | None = None,
                   case_insensitive: bool = WINDOWS) -> dict:
    """Is this usable as the sites root? {ok, error?, writable, sites[{name, state}], warnings[]}.

    `state` is per configured site: present | will_create | wrong_case. wrong_case matters: the app
    needs lowercase site folders, NAS shares often have `Corbu`. Pointing at that share unchecked would
    silently create a second, empty `corbu` beside it on a case-sensitive filesystem. On Windows
    (`case_insensitive`) `Corbu` and `corbu` are the same folder, so it is simply present.
    Accepts drive letters (Z:\\Dataset) and UNC shares (\\\\nas\\share\\Dataset) there."""
    p = Path(os.path.expanduser(path.strip())) if path and path.strip() else None
    if p is None:
        return {"ok": False, "error": "enter a folder path"}
    if not p.is_absolute():
        return {"ok": False, "error": "use a full path, e.g. "
                + ("Z:\\Dataset or \\\\nas\\share\\Dataset" if WINDOWS else "/mnt/Tori/Dataset")}
    if not p.exists():
        return {"ok": False, "error": f"{p} does not exist (is the NAS mounted?)"}
    if not p.is_dir():
        return {"ok": False, "error": f"{p} is not a folder"}
    try:
        names = [c.name for c in p.iterdir() if c.is_dir()]
    except OSError as exc:
        return {"ok": False, "error": f"cannot read {p}: {exc.strerror or exc}"}
    writable = True
    try:
        with tempfile.NamedTemporaryFile(dir=p, prefix=".write-test-"):
            pass
    except OSError:
        writable = False
    sites, warnings = [], []
    for name in site_names or []:
        if name in names or (case_insensitive and any(n.lower() == name.lower() for n in names)):
            state = "present"
        elif any(n.lower() == name.lower() for n in names):
            state = "wrong_case"
            other = next(n for n in names if n.lower() == name.lower())
            warnings.append(f"found '{other}' but this site needs the lowercase name '{name}' -- rename it "
                            f"or the app will start a separate, empty '{name}' folder")
        else:
            state = "will_create"
        sites.append({"name": name, "state": state})
    if not writable:
        warnings.append("this folder is read-only for the app: Review works, but nothing new can be "
                        "captured or saved")
    return {"ok": True, "path": str(p), "writable": writable, "sites": sites, "warnings": warnings}


# ------------------------------------------------------------------------------ store
class SettingsStore:
    """settings.yaml, loaded once and rewritten atomically on every change."""

    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH, models_dir: Path = DEFAULT_MODELS_DIR):
        self.path = Path(path)
        self.models_dir = Path(models_dir)
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        self.exists = self.path.is_file()
        if self.exists:
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                print(f"settings: could not read {self.path} ({exc}); using defaults", file=sys.stderr)
                raw = {}
            if isinstance(raw, dict):
                self._values.update({k: raw[k] for k in DEFAULTS if k in raw})

    def get(self) -> dict:
        with self._lock:
            return dict(self._values)

    def validate(self, payload: dict) -> tuple[dict, dict]:
        """(clean changes, {field: message}). Unknown keys are ignored."""
        clean, errors = {}, {}
        if "model" in payload:
            model = payload["model"]
            if model in (None, ""):
                clean["model"] = None
            elif not isinstance(model, str) or Path(model).name != model:
                errors["model"] = "pick a file from the list"
            elif resolve_model(model, self.models_dir) is None:
                errors["model"] = f"{model} is not in {self.models_dir}"
            else:
                clean["model"] = model
        if "data_dir" in payload:
            raw = payload["data_dir"]
            if raw in (None, ""):
                clean["data_dir"] = None
            elif not isinstance(raw, str):
                errors["data_dir"] = "expected a path"
            else:
                check = check_data_dir(raw)
                if check["ok"]:
                    clean["data_dir"] = check["path"]
                else:
                    errors["data_dir"] = check["error"]
        if "setup_done" in payload:
            if isinstance(payload["setup_done"], bool):
                clean["setup_done"] = payload["setup_done"]
            else:
                errors["setup_done"] = "expected true or false"
        return clean, errors

    def update(self, changes: dict) -> None:
        with self._lock:
            self._values.update(changes)
            self._write()
            self.exists = True

    def _write(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(yaml.safe_dump(self._values, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


if __name__ == "__main__":
    for m in discover_models(DEFAULT_MODELS_DIR):
        print(m)
    print(detect_devices())
