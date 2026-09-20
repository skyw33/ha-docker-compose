import asyncio
import time
from unittest.mock import patch

import pytest

from ha_docker_compose.engine import DockerEngineClient, _calculate_cpu_percent


@pytest.mark.asyncio
async def test_connect_passes_tcp_docker_host_through_unchanged() -> None:
    """The default deployment routes through docker-socket-proxy over TCP
    (see SOCKET_PROXY_SPEC.md) — connect() must pass that URL straight to
    aiodocker, not assume/prefix a unix:// socket scheme."""
    with patch("ha_docker_compose.engine.aiodocker.Docker") as mock_docker_cls:
        client = DockerEngineClient("tcp://127.0.0.1:2375")
        await client.connect()

    mock_docker_cls.assert_called_once_with(url="tcp://127.0.0.1:2375")


@pytest.mark.asyncio
async def test_connect_still_supports_direct_unix_socket() -> None:
    with patch("ha_docker_compose.engine.aiodocker.Docker") as mock_docker_cls:
        client = DockerEngineClient("unix:///var/run/docker.sock")
        await client.connect()

    mock_docker_cls.assert_called_once_with(url="unix:///var/run/docker.sock")


class _FakeContainer:
    """Stands in for an aiodocker DockerContainer: .show()/.stats() each
    sleep briefly, so a sequential-vs-concurrent regression shows up as a
    real wall-time difference in tests, the same way it did in production."""

    def __init__(self, container_id: str, delay: float = 0.05, raise_on_show: bool = False) -> None:
        self.id = container_id
        self._delay = delay
        self._raise_on_show = raise_on_show

    async def show(self) -> dict:
        await asyncio.sleep(self._delay)
        if self._raise_on_show:
            raise RuntimeError(f"inspect failed for {self.id}")
        return {
            "Id": self.id,
            "Name": f"/{self.id}",
            "Config": {"Labels": {}, "Image": "nginx:latest"},
            "State": {"Running": True, "Status": "running"},
            "Image": "sha256:abc123",
        }

    async def stats(self, stream: bool = False) -> dict:
        await asyncio.sleep(self._delay)
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200, "percpu_usage": [1]},
                "system_cpu_usage": 2000,
                "online_cpus": 1,
            },
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
            "memory_stats": {"usage": 1000, "limit": 2000},
        }


class _FakeContainersApi:
    def __init__(self, containers: list) -> None:
        self._containers = containers

    async def list(self, all: bool = False) -> list:  # noqa: A002 - matches aiodocker's own param name
        return self._containers


def _client_with_fake_containers(containers: list) -> DockerEngineClient:
    client = DockerEngineClient("tcp://127.0.0.1:2375")
    client._docker = type("_FakeDocker", (), {"containers": _FakeContainersApi(containers)})()
    return client


@pytest.mark.asyncio
async def test_list_containers_inspects_concurrently_not_sequentially() -> None:
    """10 containers each taking ~0.05s to inspect+stats (0.1s total per
    container) must complete in well under 10*0.1s=1s if gathered
    concurrently — this is the regression test for the reported 40-60s
    poll time at ~40 containers, caused by awaiting each one in sequence."""
    containers = [_FakeContainer(f"c{i}") for i in range(10)]
    client = _client_with_fake_containers(containers)

    start = time.monotonic()
    results = await client.list_containers()
    elapsed = time.monotonic() - start

    assert len(results) == 10
    assert elapsed < 0.5, f"list_containers took {elapsed:.2f}s — looks sequential, not concurrent"


@pytest.mark.asyncio
async def test_list_containers_one_failure_does_not_abort_the_rest() -> None:
    containers = [
        _FakeContainer("good1"),
        _FakeContainer("bad", raise_on_show=True),
        _FakeContainer("good2"),
    ]
    client = _client_with_fake_containers(containers)

    results = await client.list_containers()

    assert {c.id for c in results} == {"good1", "good2"}


def test_calculate_cpu_percent_also_returns_online_cpus() -> None:
    """See MULTI_SITE_IDENTITY_SPEC.md's CPU-percentage-of-host amendment
    — online_cpus is now returned (not just used internally) so it can
    become ContainerInfo.online_cpus, the fallback host-core-count source."""
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200, "percpu_usage": [1, 1]},
            "system_cpu_usage": 2000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
    }

    cpu_percent, online_cpus = _calculate_cpu_percent(stats)

    assert online_cpus == 2
    assert cpu_percent == pytest.approx((100 / 1000) * 2 * 100.0)


def test_calculate_cpu_percent_online_cpus_falls_back_to_percpu_usage_length() -> None:
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200, "percpu_usage": [1, 1, 1, 1]},
            "system_cpu_usage": 2000,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
    }

    _, online_cpus = _calculate_cpu_percent(stats)

    assert online_cpus == 4


def test_calculate_cpu_percent_missing_cpu_stats_returns_none_for_both() -> None:
    assert _calculate_cpu_percent({}) == (None, None)


@pytest.mark.asyncio
async def test_list_containers_captures_online_cpus_from_stats() -> None:
    client = _client_with_fake_containers([_FakeContainer("c1")])

    results = await client.list_containers()

    # _FakeContainer.stats() fixture reports online_cpus: 1.
    assert results[0].online_cpus == 1


class _FakeSystemApi:
    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self._info = info
        self._error = error

    async def info(self) -> dict:
        if self._error is not None:
            raise self._error
        return self._info


def _client_with_fake_system_info(info: dict | None = None, error: Exception | None = None):
    client = DockerEngineClient("tcp://127.0.0.1:2375")
    client._docker = type("_FakeDocker", (), {"system": _FakeSystemApi(info, error)})()
    return client


@pytest.mark.asyncio
async def test_get_host_cpu_count_returns_ncpu_from_info() -> None:
    client = _client_with_fake_system_info(info={"NCPU": 8})

    assert await client.get_host_cpu_count() == 8


@pytest.mark.asyncio
async def test_get_host_cpu_count_none_when_info_call_fails() -> None:
    client = _client_with_fake_system_info(error=RuntimeError("proxy down"))

    assert await client.get_host_cpu_count() is None


@pytest.mark.asyncio
async def test_get_host_cpu_count_none_when_ncpu_missing() -> None:
    client = _client_with_fake_system_info(info={})

    assert await client.get_host_cpu_count() is None


@pytest.mark.asyncio
async def test_get_host_cpu_count_none_when_ncpu_is_zero_or_invalid() -> None:
    client = _client_with_fake_system_info(info={"NCPU": 0})
    assert await client.get_host_cpu_count() is None

    client = _client_with_fake_system_info(info={"NCPU": "not-a-number"})
    assert await client.get_host_cpu_count() is None
