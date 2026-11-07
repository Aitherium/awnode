"""awnode's memory probe reports what a node's memory holds -- measured, never guessed.

The fixtures are the REAL outputs measured on 2026-11-06 from the two machines
that motivated this: an RTX 5090 desktop and a DGX Spark (GB10). The dashboard
said "usage not reported" for the Spark because GB10 answers nvidia-smi's memory
query with [N/A]; the system pool (94 of 121 GB) was the true reading and nothing
reported it.
"""

from __future__ import annotations

from awnode import memory_probe as mp

# nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu
#            --format=csv,noheader,nounits
RTX5090 = "NVIDIA GeForce RTX 5090, 17842, 32607, 13\n"
GB10 = "NVIDIA GB10, [N/A], [N/A], 3\n"

# /proc/meminfo, trimmed to the fields that matter (values in kB). 121 GB total,
# 27 GB available -> 94 GB used, the reading from the Spark that day.
MEMINFO_SPARK = (
    "MemTotal:       126877696 kB\n"
    "MemFree:          1048576 kB\n"
    "MemAvailable:    28311552 kB\n"
    "Buffers:           123456 kB\n"
)


def test_a_discrete_card_reports_its_own_vram():
    [d] = mp.parse_nvidia_smi(RTX5090)
    assert d["name"] == "NVIDIA GeForce RTX 5090"
    assert d["vram_used_mb"] == 17842
    assert d["vram_total_mb"] == 32607
    assert d["utilization_pct"] == 13.0
    assert d["unified"] is False


def test_a_unified_memory_gpu_is_unknown_not_zero():
    [d] = mp.parse_nvidia_smi(GB10)
    # [N/A] must become None -- a 0 here would put "0 GB used" on the dashboard.
    assert d["vram_used_mb"] is None
    assert d["vram_total_mb"] is None
    assert d["unified"] is True
    # Utilisation still answers, so the device is alive, not missing.
    assert d["utilization_pct"] == 3.0


def test_meminfo_used_is_total_minus_available_not_minus_free():
    m = mp.parse_meminfo(MEMINFO_SPARK)
    assert m == {"used_mb": (126877696 - 28311552) // 1024, "total_mb": 126877696 // 1024}
    # MemFree would claim ~120 GB used. MemAvailable counts reclaimable cache.
    assert 94_000 < m["used_mb"] < 97_000


def test_meminfo_without_available_is_unreadable_not_guessed():
    assert mp.parse_meminfo("MemTotal: 1000 kB\n") is None
    assert mp.parse_meminfo("") is None


def test_measure_marks_unified_when_a_gpu_shares_system_memory(monkeypatch):
    monkeypatch.setattr(mp, "_gpus", lambda: mp.parse_nvidia_smi(GB10))
    monkeypatch.setattr(mp, "_system_memory", lambda: mp.parse_meminfo(MEMINFO_SPARK))
    out = mp.measure()
    assert out["unified"] is True
    assert out["system"]["total_mb"] > 100_000
    assert out["gpu"][0]["vram_used_mb"] is None


def test_measure_on_a_discrete_card_is_not_unified(monkeypatch):
    monkeypatch.setattr(mp, "_gpus", lambda: mp.parse_nvidia_smi(RTX5090))
    monkeypatch.setattr(mp, "_system_memory", lambda: {"used_mb": 1, "total_mb": 2})
    assert mp.measure()["unified"] is False


def test_no_nvidia_smi_is_none_not_an_empty_fleet(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _name: None)
    assert mp._gpus() is None


def test_status_carries_the_memory_reading():
    # The field must be declared on the response model, or FastAPI drops it.
    from awnode.server import NodeStatus
    assert "memory" in NodeStatus.model_fields
