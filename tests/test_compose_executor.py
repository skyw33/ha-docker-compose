import pytest

from ha_docker_compose.compose import ComposeCommandError, ComposeExecutor
from ha_docker_compose.engine import (
    DetachedExecHandle,
    ExecPollResult,
    ExecResult,
    SidecarNotAvailableError,
)


class FakeEngine:
    """Stands in for DockerEngineClient: records every exec_in_container
    call and returns a canned ExecResult (or raises) per command. Also
    supports the detached-exec trio (start/poll/read_log) for
    ComposeExecutor's resumable pull/up path."""

    def __init__(self, results=None, raise_error=None):
        self.results = results or {}
        self.raise_error = raise_error
        self.calls: list[dict] = []
        self.detached_calls: list[dict] = []
        self.poll_calls: list[str] = []
        self.detached_handle = DetachedExecHandle(exec_id="exec-123", log_path="/tmp/fake.log")
        self.poll_result = ExecPollResult(running=False, exit_code=0)
        self.log_output = ""

    async def exec_in_container(self, container_name, cmd, workdir=None, timeout=60.0):
        self.calls.append(
            {"container": container_name, "cmd": cmd, "workdir": workdir, "timeout": timeout}
        )
        if self.raise_error:
            raise self.raise_error
        return self.results.get(tuple(cmd), ExecResult(exit_code=0, stdout="", stderr=""))

    async def exec_start_detached(self, container_name, cmd, workdir=None):
        self.detached_calls.append({"container": container_name, "cmd": cmd, "workdir": workdir})
        if self.raise_error:
            raise self.raise_error
        return self.detached_handle

    async def poll_exec(self, exec_id):
        self.poll_calls.append(exec_id)
        return self.poll_result

    async def read_exec_log(self, container_name, log_path):
        return self.log_output


@pytest.mark.asyncio
async def test_up_execs_into_sidecar_with_correct_workdir(tmp_path) -> None:
    engine = FakeEngine()
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    await executor.up(tmp_path)

    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call["container"] == "docker_cli_sidecar"
    assert call["cmd"] == ["docker", "compose", "up", "-d"]
    assert call["workdir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_down_never_passes_dash_v(tmp_path) -> None:
    engine = FakeEngine()
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    await executor.down(tmp_path)

    assert engine.calls[0]["cmd"] == ["docker", "compose", "down"]


@pytest.mark.asyncio
async def test_restart_with_service(tmp_path) -> None:
    engine = FakeEngine()
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    await executor.restart(tmp_path, "web")

    assert engine.calls[0]["cmd"] == ["docker", "compose", "restart", "web"]


@pytest.mark.asyncio
async def test_pull_without_service_pulls_whole_stack(tmp_path) -> None:
    engine = FakeEngine()
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    await executor.pull(tmp_path)

    assert engine.calls[0]["cmd"] == ["docker", "compose", "pull"]


@pytest.mark.asyncio
async def test_nonzero_exit_raises_compose_command_error(tmp_path) -> None:
    engine = FakeEngine(
        results={("docker", "compose", "up", "-d"): ExecResult(exit_code=1, stdout="", stderr="boom")}
    )
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    with pytest.raises(ComposeCommandError) as exc_info:
        await executor.up(tmp_path)

    assert "boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sidecar_unavailable_raises_compose_command_error(tmp_path) -> None:
    engine = FakeEngine(raise_error=SidecarNotAvailableError("sidecar not found"))
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    with pytest.raises(ComposeCommandError) as exc_info:
        await executor.up(tmp_path)

    assert "sidecar not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_compose_config_falls_back_to_raw_yaml_when_sidecar_unavailable(tmp_path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
    engine = FakeEngine(raise_error=SidecarNotAvailableError("sidecar not found"))
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    config = await executor.get_compose_config(tmp_path)

    assert config == {"services": {"web": {"image": "nginx"}}}


@pytest.mark.asyncio
async def test_get_compose_config_uses_exec_output_when_available(tmp_path) -> None:
    engine = FakeEngine(
        results={
            ("docker", "compose", "config"): ExecResult(
                exit_code=0, stdout="services:\n  web:\n    image: nginx:pinned\n", stderr=""
            )
        }
    )
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    config = await executor.get_compose_config(tmp_path)

    assert config == {"services": {"web": {"image": "nginx:pinned"}}}


@pytest.mark.asyncio
async def test_start_detached_builds_the_same_command_shape_as_run(tmp_path) -> None:
    engine = FakeEngine()
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    handle = await executor.start_detached(tmp_path, "up", "-d")

    assert handle is engine.detached_handle
    call = engine.detached_calls[0]
    assert call["container"] == "docker_cli_sidecar"
    assert call["cmd"] == ["docker", "compose", "up", "-d"]
    assert call["workdir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_start_detached_sidecar_unavailable_raises_compose_command_error(tmp_path) -> None:
    engine = FakeEngine(raise_error=SidecarNotAvailableError("sidecar not found"))
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    with pytest.raises(ComposeCommandError) as exc_info:
        await executor.start_detached(tmp_path, "up", "-d")

    assert "sidecar not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_poll_delegates_to_engine() -> None:
    engine = FakeEngine()
    engine.poll_result = ExecPollResult(running=False, exit_code=1)
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    result = await executor.poll("exec-123")

    assert result.running is False
    assert result.exit_code == 1
    assert engine.poll_calls == ["exec-123"]


@pytest.mark.asyncio
async def test_read_log_delegates_to_engine_with_sidecar_container() -> None:
    engine = FakeEngine()
    engine.log_output = "some output"
    executor = ComposeExecutor(engine, "docker_cli_sidecar")

    output = await executor.read_log("/tmp/somefile.log")

    assert output == "some output"
