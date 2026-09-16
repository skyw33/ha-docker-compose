import asyncio
from unittest.mock import patch

import aiodocker.exceptions
import pytest

from ha_docker_compose.engine import ContainerLogsUnavailableError, DockerEngineClient


class _FakeLogContainer:
    def __init__(self, lines=None, error=None, delay=0.0):
        self._lines = lines if lines is not None else []
        self._error = error
        self._delay = delay
        self.log_calls: list[dict] = []

    async def log(self, *, stdout=False, stderr=False, follow=False, tail=None, **kwargs):
        self.log_calls.append({"stdout": stdout, "stderr": stderr, "follow": follow, "tail": tail})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._lines


class _FakeContainersApi:
    def __init__(self, container) -> None:
        self._container = container

    def container(self, name):
        return self._container


def _client_with_fake_container(container) -> DockerEngineClient:
    client = DockerEngineClient("tcp://127.0.0.1:2375")
    client._docker = type("_FakeDocker", (), {"containers": _FakeContainersApi(container)})()
    return client


@pytest.mark.asyncio
async def test_get_container_logs_returns_lines_and_requests_both_streams() -> None:
    fake_container = _FakeLogContainer(lines=["line one", "line two", "line three"])
    client = _client_with_fake_container(fake_container)

    lines = await client.get_container_logs("media-jellyfin-1", tail=100)

    assert lines == ["line one", "line two", "line three"]
    call = fake_container.log_calls[0]
    assert call["stdout"] is True
    assert call["stderr"] is True
    assert call["follow"] is False  # never streams/follows — one-shot snapshot only
    assert call["tail"] == "100"


@pytest.mark.asyncio
async def test_get_container_logs_uses_custom_tail_count() -> None:
    fake_container = _FakeLogContainer(lines=[])
    client = _client_with_fake_container(fake_container)

    await client.get_container_logs("media-jellyfin-1", tail=50)

    assert fake_container.log_calls[0]["tail"] == "50"


@pytest.mark.asyncio
async def test_get_container_logs_empty_output_for_stopped_container_with_no_recent_logs() -> None:
    fake_container = _FakeLogContainer(lines=[])
    client = _client_with_fake_container(fake_container)

    lines = await client.get_container_logs("media-jellyfin-1")

    assert lines == []


@pytest.mark.asyncio
async def test_get_container_logs_docker_error_raises_container_logs_unavailable() -> None:
    error = aiodocker.exceptions.DockerError(404, {"message": "no such container"})
    fake_container = _FakeLogContainer(error=error)
    client = _client_with_fake_container(fake_container)

    with pytest.raises(ContainerLogsUnavailableError):
        await client.get_container_logs("gone-container")


@pytest.mark.asyncio
async def test_get_container_logs_timeout_raises_container_logs_unavailable() -> None:
    """Verifies the except asyncio.TimeoutError -> ContainerLogsUnavailableError
    conversion directly (patching wait_for itself) rather than actually
    waiting out the real 30s internal timeout in a unit test."""
    fake_container = _FakeLogContainer(lines=["late"])
    client = _client_with_fake_container(fake_container)

    async def _fake_wait_for(coro, timeout):
        coro.close()  # avoid an unawaited-coroutine warning from the real call
        raise asyncio.TimeoutError

    with patch("ha_docker_compose.engine.asyncio.wait_for", side_effect=_fake_wait_for):
        with pytest.raises(ContainerLogsUnavailableError):
            await client.get_container_logs("slow-container")
