#!/usr/bin/env python3
"""One detector for every camera: frames in, full-frame boxes out, several frames per call.

TWO IMPLEMENTATIONS, SAME CONTRACT  (detector.detect(frames) -> [[box, ...] per frame]):

  GpuTiledYolo   Uploads each 4K frame to the GPU once, cuts the 640px tiles there,
                 normalises there, runs the network on the whole stack and does NMS
                 there. Only the final boxes come back.
  ListTiled      Any `tiles -> detections` function (RF-DETR, or a YOLO on a machine with
                 no CUDA): tiles from all frames are joined into ONE call and the result is
                 split back per frame. Preprocessing stays wherever that function does it.

WHAT WAS MEASURED (RTX 4070 Ti, 4K frame = 32 tiles of 640, FP16, 2026-10-01):

    YOLO11s via the old list API (model.predict(tiles))      65.1 ms/frame   15.4 frames/s
    YOLO11s, GPU-resident tiling (this file)                  37.8 ms/frame   26.5 frames/s
        of which network forward                              36.0
                 NMS                                           1.4
    YOLO11s, GPU-resident, frames per call  1 / 2 / 3 / 4    37.8 / 40.8 / 41.8 / 42.7 ms/frame
    YOLO11s, list API,      frames per call  1 / 2 / 3 / 6   65.1 / 74.4 / 74.1 / 80.6 ms/frame
    RF-DETR nano, list API, frames per call  1 / 2 / 3       113.8 / 113.8 / 118.5 ms/frame

  So putting several cameras' frames into one batch does NOT raise throughput: the network
  is already compute-bound on a single frame's 32 tiles, and a bigger batch only adds memory
  (726 MiB at 1 frame, 2075 at 3) and latency. The gain is in moving the 30 ms of CPU
  preprocessing onto the GPU. `batch_frames` therefore defaults to 1; it is a parameter so
  the claim can be re-measured on other hardware, not because larger is better here.
"""
from __future__ import annotations

import time

import numpy as np

from model_infer import grid_origins, nms


def _timing_add(timing: dict | None, **parts: float) -> None:
    if timing is not None:
        for key, value in parts.items():
            timing[key] = timing.get(key, 0.0) + value


class _GpuTiled:
    """Shared by the GPU-resident detectors: tile on the GPU, run the network in chunks, bring
    back one small array. Subclasses only say how a stack of tiles becomes rows of boxes.

    `frames` may be numpy BGR (cv2 / segment replay) or GpuFrame (NVDEC); both end up as one
    (H, W, 3) uint8 RGB CUDA tensor per frame, and the tiles are views into it.
    """

    def __init__(self, tile: int, overlap: int, conf: float, merge_iou: float, device: str,
                 max_tiles_per_forward: int):
        import torch
        self.torch = torch
        self.tile, self.overlap = tile, overlap
        self.conf, self.merge_iou = conf, merge_iou
        self.device = device
        # A synchronised round of 12 cameras is 384 tiles. The frames are uploaded together,
        # but the network runs on at most this many tiles at a time, which is what bounds the
        # activation memory (see the VRAM table in this file's docstring).
        self.max_tiles = max(1, max_tiles_per_forward)
        self._origins: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self.names = {0: "bird"}

    def origins(self, w: int, h: int) -> list[tuple[int, int]]:
        key = (w, h)
        if key not in self._origins:
            self._origins[key] = grid_origins(w, h, self.tile, self.overlap)
        return self._origins[key]

    def _forward(self, tiles_u8):
        """(B, T, T, 3) uint8 RGB CUDA tensor -> (rows (N, 6) float32 [x0 y0 x1 y1 conf cls] in
        tile pixels, tile_index (N,) int64), both on the GPU."""
        raise NotImplementedError

    def detect(self, frames: list, timing: dict | None = None) -> list[list[dict]]:
        from gpu_frame import to_gpu_rgb
        torch = self.torch
        t0 = time.perf_counter()
        gpu = [to_gpu_rgb(f, self.device) for f in frames]
        refs = []                      # (frame index, ox, oy)
        for fi, g in enumerate(gpu):
            h, w = g.shape[:2]
            refs.extend((fi, ox, oy) for ox, oy in self.origins(w, h))
        if self.device.startswith("cuda"):
            torch.cuda.synchronize()
        t1 = time.perf_counter()
        rows, owner = [], []
        with torch.inference_mode():
            for i in range(0, len(refs), self.max_tiles):
                chunk = refs[i:i + self.max_tiles]
                batch = torch.stack([gpu[fi][oy:oy + self.tile, ox:ox + self.tile]
                                     for fi, ox, oy in chunk])
                r, idx = self._forward(batch)
                rows.append(r)
                owner.append(idx + i)
                del batch
            r_all = torch.cat(rows) if rows else torch.zeros((0, 6), device=self.device)
            o_all = torch.cat(owner) if owner else torch.zeros((0,), dtype=torch.long, device=self.device)
            r_cpu, o_cpu = r_all.float().cpu().numpy(), o_all.cpu().numpy()   # the only D2H
        t2 = time.perf_counter()

        per_frame: list[list[dict]] = [[] for _ in frames]
        for (x0, y0, x1, y1, conf, cls), ti in zip(r_cpu, o_cpu):
            fi, ox, oy = refs[int(ti)]
            per_frame[fi].append({"bbox": [ox + float(x0), oy + float(y0), ox + float(x1), oy + float(y1)],
                                  "conf": float(conf), "cls": self.names[int(cls)]})
        merged = [nms(boxes, self.merge_iou) for boxes in per_frame]
        t3 = time.perf_counter()
        _timing_add(timing, tile_ms=(t1 - t0) * 1000, infer_ms=(t2 - t1) * 1000,
                    nms_ms=(t3 - t2) * 1000)
        return merged


class GpuTiledYolo(_GpuTiled):
    def __init__(self, weights: str, tile: int = 640, overlap: int = 128, conf: float = 0.25,
                 tile_iou: float = 0.7, merge_iou: float = 0.5, half: bool = True,
                 device: str = "cuda", max_tiles_per_forward: int = 96):
        super().__init__(tile, overlap, conf, merge_iou, device, max_tiles_per_forward)
        from ultralytics import YOLO
        from ultralytics.utils.nms import non_max_suppression

        self._nms = non_max_suppression
        self.tile_iou = tile_iou
        self.half = half and device.startswith("cuda")
        net = YOLO(weights).model.to(device).eval()
        net = net.fuse(verbose=False) if hasattr(net, "fuse") else net   # same as predict()
        self.net = net.half() if self.half else net.float()
        self.names = net.names

    def _forward(self, tiles_u8):
        torch = self.torch
        x = tiles_u8.permute(0, 3, 1, 2)                       # NCHW, already RGB
        x = (x.half() if self.half else x.float()).div_(255.0)
        out = self.net(x)
        out = out[0] if isinstance(out, (list, tuple)) else out
        kept = self._nms(out, conf_thres=self.conf, iou_thres=self.tile_iou)
        idx = torch.cat([torch.full((len(k),), i, dtype=torch.long, device=k.device)
                         for i, k in enumerate(kept)]) if kept else torch.zeros(0, dtype=torch.long)
        rows = torch.cat(kept) if kept else torch.zeros((0, 6), device=self.device)
        return rows, idx


class GpuTiledRfdetr(_GpuTiled):
    """RF-DETR on GPU-resident tiles.

    rfdetr's own predict() was built for one image at a time: per tile it validates, copies the
    source image back to numpy, resizes, normalises and builds a `supervision.Detections` with
    several `.cpu()` syncs, so a 32-tile frame cost 114 ms of mostly Python and launch overhead
    while the network itself is small. Here a whole chunk of tiles is resized, normalised,
    run through the network and post-processed as ONE batch on the GPU, and only the surviving
    boxes come back. `half` runs the network in FP16 (rfdetr's own inference() supports it).
    """

    def __init__(self, weights: str, variant: str = "nano", tile: int = 640, overlap: int = 128,
                 conf: float = 0.25, merge_iou: float = 0.5, half: bool = True,
                 device: str = "cuda", max_tiles_per_forward: int = 32):
        super().__init__(tile, overlap, conf, merge_iou, device, max_tiles_per_forward)
        torch = self.torch
        from rfdetr import RFDETRBase, RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

        variants = {"nano": RFDETRNano, "small": RFDETRSmall, "medium": RFDETRMedium,
                    "base": RFDETRBase, "large": RFDETRLarge}
        if variant not in variants:
            raise ValueError(f"unknown RF-DETR variant {variant!r} (have: {', '.join(variants)})")
        wrapper = variants[variant](pretrain_weights=weights)
        ctx = wrapper.model
        self.half = half and device.startswith("cuda")
        self.dtype = torch.float16 if self.half else torch.float32
        self.net = ctx.model.to(device).eval().to(self.dtype)
        self.post = ctx.postprocess
        self.res = int(ctx.resolution)
        self.mean = torch.tensor(wrapper.means, device=device, dtype=self.dtype).view(1, 3, 1, 1)
        self.std = torch.tensor(wrapper.stds, device=device, dtype=self.dtype).view(1, 3, 1, 1)
        self.names = {0: "bird"}
        self._sizes: dict[int, "torch.Tensor"] = {}
        self._wrapper = wrapper          # keep alive: owns the module

    def _forward(self, tiles_u8):
        torch = self.torch
        F = torch.nn.functional
        b = tiles_u8.shape[0]
        x = tiles_u8.permute(0, 3, 1, 2).to(self.dtype).div_(255.0)
        if x.shape[-1] != self.res:                 # torchvision F.resize(antialias=False) == bilinear
            x = F.interpolate(x, size=(self.res, self.res), mode="bilinear", align_corners=False,
                              antialias=False)
        x = (x - self.mean) / self.std
        pred = self.net(x)
        if isinstance(pred, tuple):
            pred = {"pred_logits": pred[1], "pred_boxes": pred[0]}
        pred = {k: v.float() if torch.is_tensor(v) else v for k, v in pred.items()}
        if b not in self._sizes:
            self._sizes[b] = torch.tensor([[self.tile, self.tile]] * b, device=self.device)
        res = self.post(pred, target_sizes=self._sizes[b])
        scores = torch.stack([r["scores"] for r in res])            # (B, K)
        boxes = torch.stack([r["boxes"] for r in res])              # (B, K, 4)
        keep = scores >= self.conf
        idx = keep.nonzero(as_tuple=False)[:, 0]
        rows = torch.cat([boxes[keep], scores[keep][:, None], torch.zeros_like(scores[keep])[:, None]], 1)
        return rows, idx


class ListTiled:
    def __init__(self, infer_fn, tile: int = 640, overlap: int = 128, merge_iou: float = 0.5,
                 max_tiles: int = 32):
        self.infer_fn, self.tile, self.overlap, self.merge_iou = infer_fn, tile, overlap, merge_iou
        # Same bound as GpuTiledYolo: joining a whole round into one call made RF-DETR's VRAM
        # grow with the number of cameras (measured 2797 MiB peak for 3 frames), so the call is
        # cut into chunks of at most this many tiles. Throughput is unchanged (RF-DETR is
        # linear in frames: 113.8 / 113.8 / 118.5 ms per frame at 1 / 2 / 3 frames).
        self.max_tiles = max(1, max_tiles)

    def detect(self, frames: list[np.ndarray], timing: dict | None = None) -> list[list[dict]]:
        from gpu_frame import as_bgr
        t0 = time.perf_counter()
        tiles, owner = [], []
        for fi, frame in enumerate(frames):
            frame = as_bgr(frame)                      # a GpuFrame comes down to the host here
            h, w = frame.shape[:2]
            for ox, oy in grid_origins(w, h, self.tile, self.overlap):
                tiles.append(frame[oy:oy + self.tile, ox:ox + self.tile])
                owner.append((fi, ox, oy))
        t1 = time.perf_counter()
        results = []
        for i in range(0, len(tiles), self.max_tiles):
            results.extend(self.infer_fn(tiles[i:i + self.max_tiles]))
        t2 = time.perf_counter()
        per_frame: list[list[dict]] = [[] for _ in frames]
        for (fi, ox, oy), dets in zip(owner, results):
            for d in dets:
                x0, y0, x1, y1 = d["bbox"]
                per_frame[fi].append({"bbox": [ox + x0, oy + y0, ox + x1, oy + y1],
                                      "conf": d["conf"], "cls": d["cls"]})
        merged = [nms(boxes, self.merge_iou) for boxes in per_frame]
        t3 = time.perf_counter()
        _timing_add(timing, tile_ms=(t1 - t0) * 1000, infer_ms=(t2 - t1) * 1000,
                    nms_ms=(t3 - t2) * 1000)
        return merged


def build_detector(weights: str, model_type: str = "yolo", variant: str = "nano",
                   tile: int = 640, overlap: int = 128, conf: float = 0.25,
                   merge_iou: float = 0.5, half: bool = True, force_list: bool = False,
                   device: str = "auto"):
    """CUDA gets the GPU-resident path (YOLO and RF-DETR); everything else the list path.
    `force_list` selects the list path even on CUDA -- the pre-optimisation behaviour, kept so the
    two can be A/B-measured under identical service code (bench/e2e.py --extra --detector list).

    `device`: auto = CUDA when available, else CPU (the behaviour before this existed);
    cpu = never touch the GPU; cuda = the GPU or an error -- a person who asked for the GPU must
    not be silently given a CPU that is an order of magnitude slower."""
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError(f"unknown device {device!r} (auto, cpu or cuda)")
    try:
        import torch
        available = torch.cuda.is_available()
    except ImportError:
        available = False
    if device == "cuda" and not available:
        raise RuntimeError("GPU requested but CUDA is not available on this machine "
                           "(no NVIDIA driver / CPU-only torch) -- choose CPU or Auto in Settings")
    cuda = available and device != "cpu" and not force_list
    list_device = "cpu" if device == "cpu" else None     # None: let the library pick, as before
    if model_type == "rfdetr":
        if cuda:
            return GpuTiledRfdetr(weights, variant, tile, overlap, conf, merge_iou, half=half)
        from model_infer import build_infer_rfdetr
        return ListTiled(build_infer_rfdetr(weights, variant=variant, tile=tile, conf=conf,
                                            device=list_device), tile, overlap, merge_iou)
    if cuda:
        return GpuTiledYolo(weights, tile, overlap, conf, merge_iou=merge_iou, half=half)
    from model_infer import build_infer
    return ListTiled(build_infer(weights, tile=tile, conf=conf, half=False, device=list_device),
                     tile, overlap, merge_iou)
