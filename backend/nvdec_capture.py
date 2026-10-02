#!/usr/bin/env python3
"""Hardware (NVDEC) replacement for cv2.VideoCapture, same grab()/retrieve() contract.

    RTSP --tcp--> ffmpeg -c copy (demux only, ~0 CPU) --Annex-B pipe--> NVDEC --> GpuFrame

WHY
    The 12 cameras send 25 fps of 3840x2160 HEVC each: 300 fps of 4K decode. libavcodec on the
    CPU did ~100 fps in total on this machine's 8-core i9 (12 streams: 14 cores busy, 0.34x
    real time), and that, not the GPU, was what capped the fleet at about six cameras. NVDEC
    decodes ~660 fps of the same 4K HEVC at ~0.2 CPU-ms per frame (see docs/PERFORMANCE_12CAM.md).

WHY ffmpeg IS STILL IN THE PICTURE
    PyNvVideoCodec's built-in demuxer segfaults opening an rtsp:// URL here, and credentials,
    TCP transport and low-delay flags are exactly what cv2/ffmpeg already get right. So ffmpeg
    only demuxes (`-c copy`, no decode, no re-encode) and writes the elementary stream to a
    pipe; PyNvVideoCodec reads it through its callback demuxer.

CONTRACT (what CameraWorker._live_session relies on)
    isOpened()   False if the stream produced nothing within `open_timeout`
    grab()       decode the next frame on the GPU (cheap); False when the stream ended
    retrieve()   (True, GpuFrame) for the frame the last grab() produced -- converts NV12 to RGB
                 only now, so frames nobody samples are never converted
    release()    kill ffmpeg, free the decoder

    Importing this module never needs a GPU; `available()` says whether it can be used.
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import threading
import time

from gpu_frame import GpuFrame, nv12_to_rgb

PYNV = os.environ.get("PYNV_PATH")
if PYNV and PYNV not in sys.path:
    sys.path.insert(0, PYNV)


def available() -> tuple[bool, str]:
    """(usable, why not). Cheap: imports, no CUDA work."""
    try:
        import PyNvVideoCodec  # noqa: F401
        import torch
    except Exception as exc:
        return False, f"PyNvVideoCodec/torch not importable: {exc}"
    if not torch.cuda.is_available():
        return False, "no CUDA device"
    return True, ""


class NvdecCapture:
    def __init__(self, url: str, open_timeout: float = 15.0, read_timeout: float = 10.0,
                 gpu: int = 0):
        import PyNvVideoCodec as nvc
        import torch
        self.nvc, self.torch, self.gpu = nvc, torch, gpu
        self.read_timeout = read_timeout
        self.ok = False
        self.proc = None
        self._frames = None
        self._cur = None
        self.decoded = 0
        self.color_range_full = True
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-rtsp_transport", "tcp",
               "-fflags", "nobuffer", "-flags", "low_delay", "-i", url, "-map", "0:v:0",
               "-c", "copy", "-an", "-f", "mpegts", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0)
        self._fd = self.proc.stdout.fileno()
        self.last_ffmpeg_error = ""
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        self._deadline = time.monotonic() + open_timeout
        # WHO IS TO BLAME when it does not open decides what the service does about it: a camera
        # that is down ("stream") is retried as usual and says nothing about NVDEC; only a stream
        # that demuxes fine but cannot be decoded ("nvdec") is a reason to use the CPU instead.
        self.failure: str | None = "stream"
        try:
            self.demux = nvc.CreateDemuxer(self._read)
            self.failure = "nvdec"
            self.dec = nvc.CreateDecoder(
                gpuid=gpu, codec=self.demux.GetNvCodecId(), usedevicememory=True,
                outputColorType=nvc.OutputColorType.NATIVE,      # NV12; converted only when sampled
                latency=nvc.DisplayDecodeLatencyType.LOW)
            self.height, self.width = int(self.demux.Height()), int(self.demux.Width())
            self._frames = self._iter_frames()
            self.ok = True
            self.failure = None
        except Exception:
            self.release()

    def _drain_stderr(self, proc) -> None:
        """ffmpeg's log: the one place the stream's range is stated ("yuvj420p(pc, ...)"), and
        the reason a connection failed. Must be drained or ffmpeg blocks on a full pipe."""
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            m = re.search(r"Video: .*?, yuv\w+?(?:\((\w+)[,)])", line)
            if m and m.group(1) in ("pc", "tv"):
                self.color_range_full = m.group(1) == "pc"
            elif line and "Stream #" not in line and "Input #" not in line and "Metadata" not in line:
                self.last_ffmpeg_error = line[-200:]

    # the demuxer pulls bytes through this; a stalled camera must not block it forever
    def _read(self, buf) -> int:
        limit = max(self._deadline, 0) - time.monotonic()
        wait = self.read_timeout if limit <= 0 else max(0.1, min(self.read_timeout, limit))
        ready, _, _ = select.select([self._fd], [], [], wait)
        if not ready:
            return 0
        data = os.read(self._fd, len(buf))
        n = len(data)
        buf[:n] = data
        return n

    def _iter_frames(self):
        started = False
        for packet in self.demux:
            self._deadline = 0          # opened: from here on read_timeout governs
            if not started:
                # A camera is joined mid-GOP: until the first keyframe every P-frame refers to
                # pictures we never received. libavcodec drops those silently; NVDEC prints a
                # "Decode Error" per picture and would hand back garbage. Start at a keyframe.
                if not packet.key:
                    continue
                started = True
            for frame in self.dec.Decode(packet):
                yield frame

    def isOpened(self) -> bool:
        return self.ok

    def grab(self) -> bool:
        if not self.ok:
            return False
        try:
            self._cur = next(self._frames, None)
        except Exception:
            self._cur = None
        if self._cur is None:
            return False
        self.decoded += 1
        return True

    def retrieve(self):
        """(True, GpuFrame): planar RGB from the decoder, copied to an owned HWC tensor."""
        torch = self.torch
        if self._cur is None:
            return False, None
        nv12 = torch.from_dlpack(self._cur)               # zero-copy view of the decoder's surface
        rgb = nv12_to_rgb(nv12, self.height, self.width, self.color_range_full)
        torch.cuda.current_stream().synchronize()         # done reading the surface before the
        return True, GpuFrame(rgb)                        # decoder is allowed to reuse it

    def release(self) -> None:
        self.ok = False
        self._frames = None
        self._cur = None
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
        self.dec = None
        self.demux = None
