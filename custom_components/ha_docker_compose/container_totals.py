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


def _sum_running_cpu_percent(containers: Iterable[ContainerInfo]) -> float:
    """Raw (unrounded) sum of cpu_percent across running containers.

    Stopped/unavailable/unknown containers (state != "running") and a
    None cpu_percent (a rare stats-read failure) are excluded from the
    sum entirely, not counted as zero. 0.0 for no running containers
    falls out of summing an empty list — no special case needed. Kept
    unrounded so total_cpu_percent_of_host() below rounds only once,
    after dividing by the host's core count — rounding this sum first
    and then dividing again would double-round."""
    return sum(c.cpu_percent for c in containers if c.state == "running" and c.cpu_percent is not None)


def resolve_host_cpu_cores(
    containers: Iterable[ContainerInfo], configured_cores: int | None
) -> int | None:
    """The host's CPU core count to normalize the per-site CPU total
    against — see MULTI_SITE_IDENTITY_SPEC.md's CPU-percentage-of-host
    amendment.

    `configured_cores` (from DockerEngineClient.get_host_cpu_count(), the
    Engine API's /info NCPU — fetched once per entry load) is the primary
    source. If that's unavailable, falls back to the first `online_cpus`
    found among `containers` — the same value Docker's stats API already
    reports per-container (engine.py's _calculate_cpu_percent uses it
    internally); every container on one host reports the same value, so
    the first one found is authoritative. None if neither source has a
    usable value — callers must treat that as genuinely unknown, not 0
    or 1.
    """
    if configured_cores:
        return configured_cores
    for container in containers:
        if container.online_cpus:
            return container.online_cpus
    return None


def total_cpu_percent_of_host(
    containers: Iterable[ContainerInfo], host_cpu_cores: int | None
) -> float | None:
    """Sum of running containers' cpu_percent (Docker's own convention,
    where 100% is one core — so this raw sum can reach cores*100),
    divided by the host's core count and rounded to 1 decimal, so the
    result is a 0-100% figure comparable across hosts with different
    core counts. Same running/None exclusion rules as
    _sum_running_cpu_percent; 0.0 when nothing is running falls out of
    dividing 0 by a real core count.

    None (never 0.0) when host_cpu_cores is unavailable — a missing core
    count must not silently masquerade as "nothing running." Callers
    (sensor.py's TotalContainerCpuSensor) are expected to surface a None
    here as the sensor going unknown, with a logged warning, not as a
    real reading of 0%.
    """
    if not host_cpu_cores:
        return None
    return round(_sum_running_cpu_percent(containers) / host_cpu_cores, 1)


def total_cores_used(containers: Iterable[ContainerInfo]) -> float:
    """Raw sum of running containers' cpu_percent, converted from
    Docker's "100% per core" convention into a plain core count (e.g.
    1.5 means one and a half cores' worth of CPU in use), rounded to 2
    decimals. Independent of the host's core count — meaningful even
    when total_cpu_percent_of_host() above is None because host_cpu_cores
    is unavailable."""
    return round(_sum_running_cpu_percent(containers) / 100, 2)


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
