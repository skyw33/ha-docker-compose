from ha_docker_compose.container_totals import total_running_cpu_percent, total_running_memory_gb
from ha_docker_compose.engine import ContainerInfo


def _container(
    state: str = "running",
    cpu_percent: float | None = 0.0,
    memory_usage_bytes: int | None = 0,
) -> ContainerInfo:
    return ContainerInfo(
        id="abc123",
        name="test-container",
        project="media",
        service="jellyfin",
        state=state,
        health=None,
        image="jellyfin/jellyfin:latest",
        image_id="sha256:deadbeef",
        started_at=None,
        cpu_percent=cpu_percent,
        memory_usage_bytes=memory_usage_bytes,
        memory_limit_bytes=None,
    )


def test_total_running_cpu_percent_sums_running_containers_only() -> None:
    containers = [
        _container(state="running", cpu_percent=12.34),
        _container(state="running", cpu_percent=3.21),
        _container(state="exited", cpu_percent=99.0),
    ]
    assert total_running_cpu_percent(containers) == 15.6


def test_total_running_cpu_percent_excludes_none_without_treating_as_zero() -> None:
    containers = [_container(state="running", cpu_percent=None), _container(state="running", cpu_percent=10.0)]
    assert total_running_cpu_percent(containers) == 10.0


def test_total_running_cpu_percent_zero_when_nothing_running() -> None:
    containers = [_container(state="exited", cpu_percent=50.0)]
    assert total_running_cpu_percent(containers) == 0.0


def test_total_running_cpu_percent_empty_list_is_zero() -> None:
    assert total_running_cpu_percent([]) == 0.0


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
