"""GpuFrame must hand the CPU the same pixels a numpy BGR frame would be. Runs on CPU tensors, no GPU.

    python -m pytest tests/test_gpu_frame.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import cv2
import numpy as np
import pytest

torch = pytest.importorskip("torch")
from gpu_frame import GpuFrame, as_bgr, fit_width_any, rotate_frame, to_gpu_rgb  # noqa: E402


def sample(h=360, w=640, seed=1):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


def as_gpu(bgr):
    return GpuFrame(torch.from_numpy(np.ascontiguousarray(bgr[..., ::-1])))


def test_bgr_round_trip_is_exact():
    bgr = sample()
    assert np.array_equal(as_gpu(bgr).bgr(), bgr)
    assert np.array_equal(as_bgr(bgr), bgr)          # numpy frames pass straight through


def test_numpy_uploads_as_rgb():
    bgr = sample()
    rgb = to_gpu_rgb(bgr, "cpu")
    assert tuple(rgb.shape) == bgr.shape and np.array_equal(rgb.numpy(), bgr[..., ::-1])
    g = as_gpu(bgr)
    assert to_gpu_rgb(g) is g.rgb


@pytest.mark.parametrize("code", [cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180])
def test_rotate_matches_cv2(code):
    bgr = sample()
    assert np.array_equal(rotate_frame(as_gpu(bgr), code).bgr(), cv2.rotate(bgr, code))
    assert np.array_equal(rotate_frame(bgr, code), cv2.rotate(bgr, code))


def test_fit_width_is_close_to_cv2_area_resize():
    bgr = sample(2160, 3840)
    ref = fit_width_any(bgr, 1280)
    got = as_gpu(bgr).fit_width_bgr(1280)
    assert got.shape == ref.shape == (720, 1280, 3)
    assert np.abs(got.astype(int) - ref.astype(int)).mean() < 1.5     # same box filter, rounding only


def test_never_upscales():
    bgr = sample(100, 200)
    assert fit_width_any(as_gpu(bgr), 1280).shape == bgr.shape
    assert fit_width_any(bgr, 1280) is bgr


def test_shape_property_matches_numpy():
    bgr = sample(90, 160)
    assert as_gpu(bgr).shape == bgr.shape


def test_nv12_conversion_matches_opencv_limited_range():
    """BT.601 limited range, nearest chroma: OpenCV's COLOR_YUV2BGR_NV12 is the same maths."""
    from gpu_frame import nv12_to_rgb
    h, w = 64, 96
    rng = np.random.default_rng(3)
    y = rng.integers(16, 236, (h, w), dtype=np.uint8)
    uv = rng.integers(16, 241, (h // 2, w // 2, 2), dtype=np.uint8)
    nv12 = np.concatenate([y, uv.reshape(h // 2, w)], axis=0)
    ref = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
    got = nv12_to_rgb(torch.from_numpy(nv12), h, w, full_range=False).numpy()[..., ::-1]
    assert np.abs(got.astype(int) - ref.astype(int)).max() <= 2


def test_nv12_full_range_grey_is_grey():
    from gpu_frame import nv12_to_rgb
    h, w = 16, 32
    nv12 = np.full((h * 3 // 2, w), 128, np.uint8)
    nv12[:h] = 200
    out = nv12_to_rgb(torch.from_numpy(nv12), h, w, full_range=True).numpy()
    assert (out == 200).all()                      # U = V = 128 is neutral: no colour cast
