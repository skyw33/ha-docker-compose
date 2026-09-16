from unittest.mock import patch

import aiodocker.exceptions
import pytest

from ha_docker_compose.engine import DockerEngineClient, ExecNotFoundError, SidecarNotAvailableError


class _FakeExecHandle:
    """Stands in for aiodocker's Exec object as returned by container.exec()."""

    def __init__(self, exec_id: str) -> None:
        self.id = exec_id
        self.detach_calls: list[bool] = []

    async def start(self, *, detach: bool = False) -> bytes:
        self.detach_calls.append(detach)
        return b""


class _FakeContainerForExec:
    def __init__(self, exec_id: str = "exec-abc123") -> None:
        self.exec_calls: list[dict] = []
        self.exec_handle = _FakeExecHandle(exec_id)

    async def exec(self, cmd, stdout=True, stderr=True, workdir=None):
        self.exec_calls.append({"cmd": cmd, "workdir": workdir})
        return self.exec_handle


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
async def test_exec_start_detached_wraps_command_and_redirects_output() -> None:
    fake_container = _FakeContainerForExec()
    client = _client_with_fake_container(fake_container)

    handle = await client.exec_start_detached(
        "docker_cli_sidecar", ["docker", "compose", "up", "-d"], workdir="/opt/stacks/frigate"
    )

    assert handle.exec_id == "exec-abc123"
    assert handle.log_path.startswith("/tmp/ha_docker_compose_exec_")

    call = fake_container.exec_calls[0]
    assert call["workdir"] == "/opt/stacks/frigate"
    wrapped_cmd = call["cmd"]
    assert wrapped_cmd[0] == "sh"
    assert wrapped_cmd[1] == "-c"
    assert "docker compose up -d" in wrapped_cmd[2]
    assert handle.log_path in wrapped_cmd[2]
    assert ">" in wrapped_cmd[2]  # output redirected, not left attached

    # Started detached, not attached/streamed.
    assert fake_container.exec_handle.detach_calls == [True]


@pytest.mark.asyncio
async def test_exec_start_detached_generates_unique_log_paths() -> None:
    fake_container = _FakeContainerForExec()
    client = _client_with_fake_container(fake_container)

    handle1 = await client.exec_start_detached("sidecar", ["echo", "one"])
    handle2 = await client.exec_start_detached("sidecar", ["echo", "two"])

    assert handle1.log_path != handle2.log_path


class _FakeInspectableExec:
    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self._info = info
        self._error = error

    async def inspect(self) -> dict:
        if self._error is not None:
            raise self._error
        return self._info


def _client_stub() -> DockerEngineClient:
    client = DockerEngineClient("tcp://127.0.0.1:2375")
    client._docker = object()  # only needs to be non-None; AiodockerExec is patched below
    return client


@pytest.mark.asyncio
async def test_poll_exec_still_running() -> None:
    fake_exec = _FakeInspectableExec(info={"Running": True, "ExitCode": None})
    with patch("ha_docker_compose.engine.AiodockerExec", return_value=fake_exec):
        result = await _client_stub().poll_exec("exec-abc123")

    assert result.running is True
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_poll_exec_finished_success() -> None:
    fake_exec = _FakeInspectableExec(info={"Running": False, "ExitCode": 0})
    with patch("ha_docker_compose.engine.AiodockerExec", return_value=fake_exec):
        result = await _client_stub().poll_exec("exec-abc123")

    assert result.running is False
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_poll_exec_finished_failure() -> None:
    fake_exec = _FakeInspectableExec(info={"Running": False, "ExitCode": 1})
    with patch("ha_docker_compose.engine.AiodockerExec", return_value=fake_exec):
        result = await _client_stub().poll_exec("exec-abc123")

    assert result.running is False
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_poll_exec_not_found_raises_exec_not_found() -> None:
    error = aiodocker.exceptions.DockerError(404, {"message": "no such exec instance"})
    fake_exec = _FakeInspectableExec(error=error)
    with patch("ha_docker_compose.engine.AiodockerExec", return_value=fake_exec):
        with pytest.raises(ExecNotFoundError):
            await _client_stub().poll_exec("exec-abc123")


@pytest.mark.asyncio
async def test_poll_exec_transient_error_raises_sidecar_not_available_not_exec_not_found() -> None:
    """A non-404 error (proxy blip, timeout, 500, etc.) means "couldn't
    check right now," not "the command is gone" — pull_jobs.py retries on
    this, but would wrongly abandon the job if it were misreported as
    ExecNotFoundError."""
    error = aiodocker.exceptions.DockerError(503, {"message": "service unavailable"})
    fake_exec = _FakeInspectableExec(error=error)
    with patch("ha_docker_compose.engine.AiodockerExec", return_value=fake_exec):
        with pytest.raises(SidecarNotAvailableError):
            await _client_stub().poll_exec("exec-abc123")


@pytest.mark.asyncio
async def test_read_exec_log_returns_captured_output() -> None:
    # read_exec_log is a thin wrapper around exec_in_container (cat, then
    # rm) — patch that directly to isolate this method's own logic (which
    # response feeds the return value, error-swallowing) from
    # exec_in_container's own already-tested behavior.
    client = _client_stub()

    async def fake_exec_in_container(container_name, cmd, workdir=None, timeout=60.0):
        from ha_docker_compose.engine import ExecResult

        if cmd[0] == "cat":
            return ExecResult(exit_code=0, stdout="log contents here", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    client.exec_in_container = fake_exec_in_container  # type: ignore[method-assign]

    output = await client.read_exec_log("docker_cli_sidecar", "/tmp/somefile.log")

    assert output == "log contents here"


@pytest.mark.asyncio
async def test_read_exec_log_is_best_effort_on_failure() -> None:
    client = _client_stub()

    async def failing_exec_in_container(*args, **kwargs):
        raise RuntimeError("sidecar unreachable")

    client.exec_in_container = failing_exec_in_container  # type: ignore[method-assign]

    output = await client.read_exec_log("docker_cli_sidecar", "/tmp/somefile.log")

    assert output == ""
