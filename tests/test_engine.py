import asyncio
import time
from unittest.mock import patch

import pytest

from ha_docker_compose.engine import DockerEngineClient


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
