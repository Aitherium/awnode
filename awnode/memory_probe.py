"""What this node's memory actually holds, measured here.

Why this exists: a dashboard can only show a number something measured. For a
discrete card (an RTX 5090) the scheduler's ALLOCATION is easy to read and the
card's real usage is one `nvidia-smi` away. For a unified-memory part (a DGX
Spark / GB10), `nvidia-smi --query-gpu=memory.used` answers ``[N/A]``: the GPU
and the CPU share one pool, so the only honest reading is the system's own
memory accounting. Nothing on such a node reported it, so the platform
dashboard said "usage not reported" while the box sat at 94 of 121 GB.

Two readings, never merged:

  ``gpu``     per-device VRAM from nvidia-smi. A device that answers ``[N/A]``
              is listed with ``vram_used_mb: None`` and ``unified: True`` --
              unknown, not zero.
  ``system``  host RAM from /proc/meminfo (Linux) or the OS (other platforms).
              On a unified-memory node THIS is the GPU's working memory.

Every value is either measured or ``None``. Nothing is estimated, and nothing
falls back to a configured total: a probe that cannot read reports that it
could not read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

# nvidia-smi on a busy box can stall; the dashboard polls, so never wait long.
_NVIDIA_SMI_TIMEOUT_S = 4.0
_NA = {"[n/a]", "n/a", "[not supported]", "not supported", ""}


def _num(field: str) -> Optional[float]:
    """A numeric nvidia-smi field, or None for [N/A] / unparseable."""
    s = field.strip()
    if s.lower() in _NA:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_nvidia_smi(csv_text: str) -> List[Dict[str, Any]]:
    """Parse `nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu
    --format=csv,noheader,nounits`. Pure, so it is tested without a GPU.

    A row whose memory fields are [N/A] is a unified-memory device: kept, with
    ``unified: True`` and the memory fields ``None``.
    """
    devices: List[Dict[str, Any]] = []
    for line in csv_text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        name = parts[0]
        used, total, util = _num(parts[1]), _num(parts[2]), _num(parts[3])
        devices.append({
            "name": name,
            "vram_used_mb": int(used) if used is not None else None,
            "vram_total_mb": int(total) if total is not None else None,
            "utilization_pct": util,
            # [N/A] memory with a live utilisation reading = the GPU shares the
            # system pool; read memory from `system` instead.
            "unified": used is None and total is None,
        })
    return devices


def parse_meminfo(text: str) -> Optional[Dict[str, int]]:
    """Parse /proc/meminfo into used/total MB. Pure. ``None`` if unreadable.

    Used = MemTotal - MemAvailable: the kernel's own estimate of memory a new
    workload can get without swapping, which is what "how full is it" means.
    (MemFree alone ignores reclaimable page cache and reads far too high.)
    """
    fields: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        tok = rest.strip().split()
        if tok and tok[0].isdigit():
            fields[key.strip()] = int(tok[0])  # kB
    total_kb = fields.get("MemTotal")
    avail_kb = fields.get("MemAvailable")
    if not total_kb or avail_kb is None:
        return None
    return {
        "used_mb": (total_kb - avail_kb) // 1024,
        "total_mb": total_kb // 1024,
    }


def _system_memory() -> Optional[Dict[str, int]]:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                return parse_meminfo(fh.read())
        except OSError:
            return None
    # Not Linux: psutil when present; otherwise unknown rather than invented.
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    vm = psutil.virtual_memory()
    return {"used_mb": int((vm.total - vm.available) // (1024 * 1024)),
            "total_mb": int(vm.total // (1024 * 1024))}


def _gpus() -> Optional[List[Dict[str, Any]]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        # A console-less parent must not flash a terminal for a status poll.
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=_NVIDIA_SMI_TIMEOUT_S, check=False, **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return parse_nvidia_smi(out.stdout)


def measure() -> Dict[str, Any]:
    """This node's memory, measured now. Each half is ``None`` when unreadable."""
    gpus = _gpus()
    system = _system_memory()
    return {
        "gpu": gpus,
        "system": system,
        # True when at least one GPU shares system memory: the reader should take
        # `system` as that GPU's working memory instead of showing "unknown".
        "unified": bool(gpus) and any(d.get("unified") for d in gpus),
    }
