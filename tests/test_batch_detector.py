"""GpuTiledYolo must find what the old list-API path finds, and give the same answer for a
frame whether it is alone in a batch or among others.  Needs CUDA, the production weights and
the imported drone frames; skipped (pytest) / reported (script) when they are absent.

    python tests/test_batch_detector.py
"""
import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

WEIGHTS = ROOT.parent / "sites/combined/dataset/runs/finetune-2026-09-11-reviewed-balanced/weights/best.pt"
FRAMES = sorted(glob.glob(str(ROOT / "sites/babadag/frames/2026-09-30-drona/*/frame_3.jpg")))[:24]


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter + 1e-9)


def compare(ref, got, min_iou=0.85):
    """Greedy one-to-one match; returns (matched, only_ref, only_got)."""
    left = list(got); matched = 0
    for r in ref:
        best = max(left, key=lambda g: iou(r["bbox"], g["bbox"]), default=None)
        if best is not None and iou(r["bbox"], best["bbox"]) >= min_iou and abs(r["conf"] - best["conf"]) < 0.06:
            left.remove(best); matched += 1
    return matched, len(ref) - matched, len(left)


def run():
    import cv2, torch
    from batch_detector import GpuTiledYolo
    from model_infer import build_infer, detect_frame
    images = [cv2.imread(p) for p in FRAMES]
    ref_fn = build_infer(str(WEIGHTS), tile=640, conf=0.25, half=True)
    ref = [detect_frame(im, 640, ref_fn, 128, 0.5) for im in images]
    det = GpuTiledYolo(str(WEIGHTS))
    single = [det.detect([im])[0] for im in images]
    batched = []
    for i in range(0, len(images), 3):
        batched.extend(det.detect(images[i:i + 3]))
    tot = lambda cmp: [sum(x) for x in zip(*cmp)]
    vs_ref = tot([compare(r, s) for r, s in zip(ref, single)])
    vs_batch = tot([compare(s, b, 0.98) for s, b in zip(single, batched)])
    print(f"{len(images)} frames | reference boxes {sum(map(len, ref))}")
    print(f"gpu-tiled vs list-API : matched {vs_ref[0]}, only in reference {vs_ref[1]}, only in gpu {vs_ref[2]}")
    print(f"alone vs in a batch   : matched {vs_batch[0]}, differ {vs_batch[1] + vs_batch[2]}")
    return vs_ref, vs_batch, sum(map(len, ref))


RFDETR = ROOT.parent / "sites/combined/dataset/runs/rfdetr-nano/checkpoint_best_ema.pth"
ALL_FRAMES = sorted(glob.glob(str(ROOT / "sites/babadag/frames/2026-09-30-drona/*/frame_*.jpg")))[::3][:60]


def run_rfdetr():
    """GpuTiledRfdetr (FP32 and FP16) against the old rfdetr.predict() list path on real frames."""
    import cv2
    from batch_detector import GpuTiledRfdetr, ListTiled
    from model_infer import build_infer_rfdetr
    images = [cv2.imread(p) for p in ALL_FRAMES]
    ref = ListTiled(build_infer_rfdetr(str(RFDETR), "nano", 640, 0.25), 640, 128, 0.5)
    ref_boxes = [ref.detect([im])[0] for im in images]
    out = {}
    for name, half in (("fp32", False), ("fp16", True)):
        det = GpuTiledRfdetr(str(RFDETR), "nano", half=half)
        single = [det.detect([im])[0] for im in images]
        batched = []
        for i in range(0, len(images), 12):
            batched.extend(det.detect(images[i:i + 12]))
        vs_ref = [sum(x) for x in zip(*[compare(r, s) for r, s in zip(ref_boxes, single)])]
        vs_batch = [sum(x) for x in zip(*[compare(s, b, 0.98) for s, b in zip(single, batched)])]
        print(f"rfdetr gpu-{name}: {len(images)} frames, reference boxes {sum(map(len, ref_boxes))} | "
              f"matched {vs_ref[0]}, only in reference {vs_ref[1]}, only in gpu {vs_ref[2]} | "
              f"alone vs batched: differ {vs_batch[1] + vs_batch[2]}")
        out[name] = (vs_ref, vs_batch, sum(map(len, ref_boxes)))
    return out


def test_rfdetr_parity():
    import pytest
    try:
        import torch
        ok = torch.cuda.is_available() and RFDETR.exists() and len(ALL_FRAMES) >= 6
    except ImportError:
        ok = False
    if not ok:
        pytest.skip("needs CUDA, the RF-DETR checkpoint and the drone frames")
    for name, (vs_ref, vs_batch, n) in run_rfdetr().items():
        assert vs_ref[1] + vs_ref[2] <= max(2, 0.05 * n), name
        assert vs_batch[1] + vs_batch[2] == 0, name


def test_parity():
    import pytest
    try:
        import torch
        ok = torch.cuda.is_available() and WEIGHTS.exists() and len(FRAMES) >= 6
    except ImportError:
        ok = False
    if not ok:
        pytest.skip("needs CUDA, production weights and the drone frames")
    vs_ref, vs_batch, n = run()
    assert vs_ref[1] + vs_ref[2] <= max(2, 0.03 * n)     # <=3% of boxes may differ (fp16 rounding)
    assert vs_batch[1] + vs_batch[2] == 0                # a frame's answer does not depend on its neighbours


if __name__ == "__main__":
    run()
    run_rfdetr()
