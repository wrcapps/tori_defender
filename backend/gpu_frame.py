#!/usr/bin/env python3
"""A decoded frame that stays on the GPU, and the few places the CPU still needs a picture.

WHY THIS EXISTS
    A 4K frame is 25 MB as BGR. Decoding it on the CPU, converting to BGR, uploading it for the
    detector, and (for the live view) resizing it on the CPU again moved it through host memory
    three times, and the HEVC decode itself was what saturated the CPU (see docs/PERFORMANCE_12CAM.md).
    With NVDEC the picture is born on the GPU and the detector tiles it there; it only comes down
    to the host when something really needs pixels:

        preview      resized to ~1280 px ON the GPU first, so ~2.7 MB comes down, not 25 MB
        saved frame  full 4K, but only while a camera is recording a window
        crop         a 640 px patch around a detection

    `GpuFrame.rgb` is a (H, W, 3) uint8 CUDA tensor in RGB order. Everything else is derived.
    numpy BGR frames (the cv2 / segment-replay path) keep working everywhere a GpuFrame does,
    through `as_bgr()` and `to_gpu_rgb()`.
"""
from __future__ import annotations

import threading

import cv2
import numpy as np


class GpuFrame:
    __slots__ = ("rgb", "_bgr")

    def __init__(self, rgb):
        self.rgb = rgb                  # (H, W, 3) uint8, RGB, on the GPU
        self._bgr: np.ndarray | None = None

    @property
    def shape(self) -> tuple[int, int, int]:
        return tuple(self.rgb.shape)

    def bgr(self) -> np.ndarray:
        """Full-resolution BGR on the host (cached). Only for saving frames and cutting crops."""
        if self._bgr is None:
            self._bgr = np.ascontiguousarray(self.rgb.flip(-1).cpu().numpy())
        return self._bgr

    def fit_width_bgr(self, width: int) -> np.ndarray:
        """BGR resized so its longest edge is `width`, resized on the GPU before the copy down."""
        import torch
        h, w = self.rgb.shape[:2]
        longest = max(h, w)
        if longest <= width:
            return self.bgr()
        k = width / longest
        nh, nw = max(1, round(h * k)), max(1, round(w * k))
        small = torch.nn.functional.interpolate(
            self.rgb.permute(2, 0, 1)[None].float(), size=(nh, nw), mode="area")
        return np.ascontiguousarray(small[0].permute(1, 2, 0).flip(-1).round().clamp_(0, 255)
                                    .to(torch.uint8).cpu().numpy())


def as_bgr(frame) -> np.ndarray:
    return frame.bgr() if isinstance(frame, GpuFrame) else frame


def fit_width_any(frame, width: int) -> np.ndarray:
    if isinstance(frame, GpuFrame):
        return frame.fit_width_bgr(width)
    longest = max(frame.shape[:2])
    if longest <= width:
        return frame
    k = width / longest
    return cv2.resize(frame, (round(frame.shape[1] * k), round(frame.shape[0] * k)),
                      interpolation=cv2.INTER_AREA)


def to_gpu_rgb(frame, device: str = "cuda"):
    """(H, W, 3) uint8 RGB CUDA tensor for either kind of frame."""
    import torch
    if isinstance(frame, GpuFrame):
        return frame.rgb
    return torch.from_numpy(np.ascontiguousarray(frame)).to(device, non_blocking=True).flip(-1)


def rotate_frame(frame, cv2_code: int):
    """cv2.rotate for either kind of frame (Corbu is mounted on its side)."""
    if not isinstance(frame, GpuFrame):
        return cv2.rotate(frame, cv2_code)
    import torch
    k = {cv2.ROTATE_90_COUNTERCLOCKWISE: 1, cv2.ROTATE_90_CLOCKWISE: -1, cv2.ROTATE_180: 2}[cv2_code]
    return GpuFrame(torch.rot90(frame.rgb, k, dims=(0, 1)).contiguous())


_NV12_BANDS = 4
_nv12_lock = threading.Lock()


def nv12_to_rgb(nv12, height: int, width: int, full_range: bool = True) -> "torch.Tensor":
    """(H*3/2, W) uint8 NV12 -> (H, W, 3) uint8 RGB, on whatever device `nv12` is on.

    BT.601 with nearest-neighbour chroma, which is exactly what the CPU path (OpenCV's ffmpeg
    capture, i.e. swscale defaults) does to these cameras' yuvj420p streams -- NOT what NVDEC's
    own RGB output does (that is BT.709: +2.3/255 mean error on this footage, a visible colour
    cast). Measured against cv2 on a real 4K frame: mean abs diff 0.64/255, max 2 (rounding).
    The detector was trained on frames converted the swscale way, so this is the one to match.

    Done in row bands, one conversion at a time, so a 12-camera fleet does not hold twelve
    4K float32 temporaries at once (~70 MB peak instead of ~1 GB).
    """
    import torch
    out = torch.empty((height, width, 3), dtype=torch.uint8, device=nv12.device)
    band = max(2, (height // _NV12_BANDS) // 2 * 2)
    y_scale, c_scale, y_off = (1.0, 1.0, 0.0) if full_range else (255 / 219, 255 / 224, 16.0)
    with _nv12_lock:
        for r0 in range(0, height, band):
            r1 = min(height, r0 + band)
            y = (nv12[r0:r1].float() - y_off) * y_scale
            uv = nv12[height + r0 // 2: height + r1 // 2].reshape((r1 - r0) // 2, width // 2, 2)
            u = (uv[..., 0].float() - 128.0) * c_scale
            v = (uv[..., 1].float() - 128.0) * c_scale

            def up(c):                           # nearest-neighbour 2x in both directions
                return c[:, None, :, None].expand(-1, 2, -1, 2).reshape(r1 - r0, width)
            u, v = up(u), up(v)
            r = y + 1.402 * v
            g = y - 0.344136 * u - 0.714136 * v
            b = y + 1.772 * u
            out[r0:r1] = torch.stack((r, g, b), dim=-1).round_().clamp_(0, 255).to(torch.uint8)
    return out
