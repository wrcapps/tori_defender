"""The layout in docs/DATA_LAYOUT.md is only real if nothing builds those paths by hand."""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from layout import SiteLayout

# layout.py / lifecycle.py own the tree; the importer writes confirmed windows by design.
ALLOWED = {"layout.py", "lifecycle.py", "import_excel_frames.py"}
HAND_BUILT = re.compile(r"""site_dir\([^)]*["'](frames|inbox|trash)["']|/\s*["'](inbox|trash)["']""")


def test_no_module_builds_window_roots_by_hand():
    offenders = []
    for path in sorted((ROOT / "backend").glob("*.py")):
        if path.name in ALLOWED:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HAND_BUILT.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "go through layout.SiteLayout instead:\n" + "\n".join(offenders)


def test_doc_names_every_directory_the_code_knows():
    doc = (ROOT / "docs" / "DATA_LAYOUT.md").read_text(encoding="utf-8")
    import layout
    for name in (layout.LIVE, layout.INBOX, layout.FRAMES, layout.TRASH, layout.LIFECYCLE_LOG, *layout.BUCKET_FILES):
        assert name in doc, f"{name} is part of the layout but docs/DATA_LAYOUT.md never mentions it"


def test_capture_windows_open_in_the_inbox_with_a_unique_identity(tmp_path):
    import acquisition_service as svc
    lay = SiteLayout(tmp_path)
    lay.ensure()
    now = datetime(2026, 10, 25, 3, 30, 0)
    first = svc.Window(lay.inbox, "cam", now, lay)
    assert first.dir.parent.parent == lay.inbox
    first.close()
    lay.move(first.day, first.name, lay.frames)                 # it was confirmed...
    again = svc.Window(lay.inbox, "cam", now, lay)             # ...and the clock repeats the same second
    assert again.name != first.name and not (lay.frames / again.day / again.name).exists()
