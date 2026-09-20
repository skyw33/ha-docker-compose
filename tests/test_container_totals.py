from ha_docker_compose.container_totals import (
    resolve_host_cpu_cores,
    total_cores_used,
    total_cpu_percent_of_host,
    total_running_memory_gb,
)
from ha_docker_compose.engine import ContainerInfo


def _container(
    state: str = "running",
    cpu_percent: float | None = 0.0,
    memory_usage_bytes: int | None = 0,
    online_cpus: int | None = None,
    project: str | None = "media",
) -> ContainerInfo:
    return ContainerInfo(
        id="abc123",
        name="test-container",
        project=project,
        service="jellyfin",
        state=state,
        health=None,
        image="jellyfin/jellyfin:latest",
        image_id="sha256:deadbeef",
        started_at=None,
        cpu_percent=cpu_percent,
        online_cpus=online_cpus,
        memory_usage_bytes=memory_usage_bytes,
        memory_limit_bytes=None,
    )


# --- resolve_host_cpu_cores ---------------------------------------------


def test_resolve_host_cpu_cores_prefers_configured_over_online_cpus() -> None:
    containers = [_container(online_cpus=2)]
    assert resolve_host_cpu_cores(containers, configured_cores=4) == 4


def test_resolve_host_cpu_cores_falls_back_to_online_cpus_when_configured_missing() -> None:
    containers = [_container(online_cpus=2)]
    assert resolve_host_cpu_cores(containers, configured_cores=None) == 2


def test_resolve_host_cpu_cores_falls_back_to_first_container_reporting_online_cpus() -> None:
    containers = [_container(online_cpus=None), _container(online_cpus=4)]
    assert resolve_host_cpu_cores(containers, configured_cores=None) == 4


def test_resolve_host_cpu_cores_none_when_both_sources_unavailable() -> None:
    containers = [_container(online_cpus=None)]
    assert resolve_host_cpu_cores(containers, configured_cores=None) is None


def test_resolve_host_cpu_cores_treats_zero_configured_as_missing() -> None:
    # A configured core count of 0 is nonsensical, not "the host has zero
    # CPUs" — fall back the same as if it were None.
    containers = [_container(online_cpus=2)]
    assert resolve_host_cpu_cores(containers, configured_cores=0) == 2


# --- total_cpu_percent_of_host -------------------------------------------


def test_total_cpu_percent_of_host_two_core_host() -> None:
    containers = [
        _container(state="running", cpu_percent=50.0),
        _container(state="running", cpu_percent=50.0),
    ]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=2) == 50.0


def test_total_cpu_percent_of_host_four_core_host_with_container_over_100_percent() -> None:
    # A single container using 2.5 cores' worth (Docker's own convention:
    # >100% is normal on a multi-core host) on a 4-core host is 62.5% of
    # the whole host.
    containers = [_container(state="running", cpu_percent=250.0)]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=4) == 62.5


def test_total_cpu_percent_of_host_excludes_stopped_and_unavailable_containers() -> None:
    containers = [
        _container(state="running", cpu_percent=100.0),
        _container(state="exited", cpu_percent=999.0),
        _container(state="unavailable", cpu_percent=999.0),
    ]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=2) == 50.0


def test_total_cpu_percent_of_host_includes_containers_from_multiple_stacks_and_orphans() -> None:
    # The function has no notion of "stack" at all — it just sums
    # whatever ContainerInfo list it's given. An orphaned pseudo-stack's
    # containers land in that same flat list upstream (coordinator.py),
    # so nothing here needs to treat them specially for them to count.
    containers = [
        _container(state="running", cpu_percent=50.0, project="stack-a"),
        _container(state="running", cpu_percent=25.0, project="stack-b"),
        _container(state="running", cpu_percent=25.0, project=None),  # orphaned
    ]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=2) == 50.0


def test_total_cpu_percent_of_host_zero_when_nothing_running() -> None:
    containers = [_container(state="exited", cpu_percent=80.0)]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=4) == 0.0


def test_total_cpu_percent_of_host_none_when_core_count_missing() -> None:
    # Never 0.0 — a missing core count must not masquerade as "nothing
    # running."
    containers = [_container(state="running", cpu_percent=50.0)]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=None) is None


def test_total_cpu_percent_of_host_rounds_to_one_decimal() -> None:
    containers = [_container(state="running", cpu_percent=10.0)]
    assert total_cpu_percent_of_host(containers, host_cpu_cores=3) == round(10.0 / 3, 1)


# --- total_cores_used ------------------------------------------------------


def test_total_cores_used_converts_percent_sum_to_core_count() -> None:
    containers = [
        _container(state="running", cpu_percent=100.0),
        _container(state="running", cpu_percent=50.0),
    ]
    assert total_cores_used(containers) == 1.5


def test_total_cores_used_zero_when_nothing_running() -> None:
    assert total_cores_used([]) == 0.0


# --- total_running_memory_gb (unchanged by this change) --------------------


def test_total_running_memory_gb_converts_bytes_to_gb() -> None:
    one_gb = 1024**3
    containers = [
        _container(state="running", memory_usage_bytes=one_gb),
        _container(state="running", memory_usage_bytes=one_gb // 2),
    ]
    assert total_running_memory_gb(containers) == 1.5


def test_total_running_memory_gb_excludes_stopped_containers() -> None:
    one_gb = 1024**3
    containers = [
        _container(state="running", memory_usage_bytes=one_gb),
        _container(state="exited", memory_usage_bytes=one_gb * 10),
        _container(state="unavailable", memory_usage_bytes=one_gb * 10),
    ]
    assert total_running_memory_gb(containers) == 1.0


def test_total_running_memory_gb_rounds_to_two_decimals() -> None:
    containers = [_container(state="running", memory_usage_bytes=1_500_000_000)]
    assert total_running_memory_gb(containers) == round(1_500_000_000 / 1024**3, 2)


def test_total_running_memory_gb_zero_when_nothing_running() -> None:
    assert total_running_memory_gb([]) == 0.0
