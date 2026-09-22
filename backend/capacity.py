#!/usr/bin/env python3
"""How much tiled inference a camera fleet asks of one GPU, and the ceiling it
has to fit under.

Shared by live_capture_all.py (checked once, against a fixed list, at startup)
and app.py's on-demand capture manager (checked before starting one more
camera, against whatever is already running) -- one set of numbers, so the two
never quietly disagree about what the GPU can actually do.
"""
from __future__ import annotations

import subprocess

# Measured on this pipeline's own footage: 32 tiles per 4K frame, FP16, one
# RTX 4070 Ti. Override with --gpu-fps on a different card.
MEASURED_GPU_FPS = 6.5

# Measured loading this project's own finetuned YOLO11s: each process's model
# weights plus its own CUDA context cost roughly this much, before it infers a
# single frame. This machine's GPU is not necessarily this app's alone --
# other training runs and unrelated jobs share it -- so what matters is FREE
# memory measured right now, not a fixed camera count.
MIN_FREE_MIB_TO_START = 1400


def demand(camera) -> tuple[float, float]:
    """(idle, in-window) inference frames per second this camera asks of the GPU."""
    live = camera.live
    return (float(live["scan_fps"]),
            float(live["save_fps"]) / max(1, int(live["infer_every_n"])))


def total_demand(cameras) -> tuple[float, float]:
    """(idle, worst-case) demand summed across every camera given."""
    pairs = [demand(c) for c in cameras]
    return sum(p[0] for p in pairs), sum(p[1] for p in pairs)


def budget_warning(cameras, gpu_fps: float = MEASURED_GPU_FPS) -> str | None:
    """None if `cameras` fits in `gpu_fps`, else a message explaining why not.

    Going over does not crash anything -- each process just falls behind its
    own sampling interval and writes thinner windows than configured, which is
    hard to notice until the playback looks wrong much later. So the arithmetic
    is done up front and handed back as a warning, not silently absorbed.
    """
    idle, busy = total_demand(cameras)
    if busy <= gpu_fps:
        return None
    message = (f"{len(cameras)} camera(s) would ask for {busy:.1f} tiled "
              f"inferences/second at peak, but this GPU does about {gpu_fps:g}. "
              f"Nothing will crash -- affected cameras will just fall behind and "
              f"write sparser windows than configured.")
    if idle > gpu_fps:
        message += f" Even idle scanning alone asks for {idle:.1f}/s."
    return message


def gpu_free_mib() -> int | None:
    """Free VRAM on GPU 0 right now, or None if it can't be measured (no
    nvidia-smi, no GPU, a driver hiccup) -- callers must treat None as "cannot
    check", not as "zero free", or a machine with no GPU visibility would
    refuse to start anything.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        return int(out.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def vram_refusal(min_free_mib: int = MIN_FREE_MIB_TO_START) -> str | None:
    """None if there is enough free VRAM to start one more capture process,
    else a message explaining why not.

    Unlike the FPS budget above, this is a hard stop rather than a warning:
    going over the FPS budget degrades quietly (thinner windows, choppier
    preview), but starting a process into VRAM that is already spoken for
    doesn't degrade -- it crashes with CUDA out-of-memory, immediately, having
    already paid the cost of loading the model. Measured directly: on a
    12 GB card already carrying other jobs, six camera processes plus two
    unrelated training runs left under 900 MiB free, and the next camera to
    start died in the middle of its first frame.
    """
    free = gpu_free_mib()
    if free is None:
        return None
    if free < min_free_mib:
        return (f"only {free} MiB of GPU memory is free (this GPU may be shared with "
                f"other jobs); starting another camera needs roughly {min_free_mib} MiB "
                f"and would very likely crash with a CUDA out-of-memory error instead")
    return None
