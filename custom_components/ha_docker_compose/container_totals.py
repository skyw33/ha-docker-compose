"""Pure per-entry container CPU/memory total math — see
MULTI_SITE_IDENTITY_SPEC.md. Depends only on engine.py's ContainerInfo
(no homeassistant imports), same rationale as site_identity.py: the
sum/exclusion/rounding/unit-conversion logic is unit tested directly,
without a running Home Assistant instance.
"""
from __future__ import annotations

from collections.abc import Iterable

from .engine import ContainerInfo

_BYTES_PER_GB = 1024**3


def total_running_cpu_percent(containers: Iterable[ContainerInfo]) -> float:
    """Sum of cpu_percent across running containers, rounded to 1 decimal.

    Stopped/unavailable/unknown containers (state != "running") and a
    None cpu_percent (a rare stats-read failure) are excluded from the
    sum entirely, not counted as zero. 0.0 for no running containers
    falls out of summing an empty list — no special case needed."""
    values = [c.cpu_percent for c in containers if c.state == "running" and c.cpu_percent is not None]
    return round(sum(values), 1)


def total_running_memory_gb(containers: Iterable[ContainerInfo]) -> float:
    """Sum of memory_usage_bytes across running containers, converted from
    bytes to GB and rounded to 2 decimals. Same exclusion rules as
    total_running_cpu_percent."""
    values = [
        c.memory_usage_bytes
        for c in containers
        if c.state == "running" and c.memory_usage_bytes is not None
    ]
    return round(sum(values) / _BYTES_PER_GB, 2)
