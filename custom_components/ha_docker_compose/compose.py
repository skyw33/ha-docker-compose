"""Compose command execution via Docker exec into a sidecar container.

Compose commands are no longer run as local subprocesses of the Home
Assistant process — the stock `ghcr.io/home-assistant/home-assistant`
image must stay unmodified, and doesn't ship the `docker` CLI. Instead, a
sidecar container (a `docker:cli`-family image, name configurable — see
const.CONF_SIDECAR_CONTAINER) has the Docker socket and the stacks root
mounted at the identical host path as everywhere else, and does nothing but
sit idle so there's always a running container with real `docker`/`docker
compose` binaries to exec into. See SIDECAR_EXEC_SPEC.md.

No path translation happens here: because the sidecar mounts the stacks
root at the same absolute path as the host, a stack's real folder path
(from discovery.py's filesystem scan) is valid unchanged as the exec's
working directory — `docker compose` auto-discovers the compose file there
exactly as it would from a host shell in that directory, `.env`
substitution included.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .const import COMPOSE_FILENAMES
from .engine import (
    DetachedExecHandle,
    DockerEngineClient,
    ExecPollResult,
    SidecarNotAvailableError,
)

_LOGGER = logging.getLogger(__name__)


class ComposeCommandError(Exception):
    """Raised when a `docker compose` invocation (run inside the sidecar)
    exits non-zero, or the sidecar itself couldn't be reached at all."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"docker compose {' '.join(args)} failed (exit {returncode}): {stderr.strip()}"
        )


class ComposeExecutor:
    """Bound to one (engine connection, sidecar container name) pair —
    construct once per config entry and reuse for every compose command."""

    def __init__(self, engine: DockerEngineClient, sidecar_container: str) -> None:
        self._engine = engine
        self._sidecar_container = sidecar_container

    async def _run(self, stack_dir: Path, *args: str, timeout: float = 60.0) -> str:
        cmd = ["docker", "compose", *args]
        try:
            result = await self._engine.exec_in_container(
                self._sidecar_container, cmd, workdir=str(stack_dir), timeout=timeout
            )
        except SidecarNotAvailableError as err:
            raise ComposeCommandError(list(args), -1, str(err)) from err

        if result.exit_code != 0:
            raise ComposeCommandError(list(args), result.exit_code, result.stderr)

        return result.stdout

    async def get_compose_config(self, stack_dir: Path) -> dict[str, Any]:
        """Return the fully resolved compose config for a stack.

        Always prefer `docker compose config` (run inside the sidecar) over
        reading the raw YAML directly, so `.env` substitution and Compose's
        own merge/normalization rules are respected. Raw YAML read is only
        a fallback if the exec invocation fails (sidecar not
        found/running, or a malformed compose file the CLI itself can't
        parse in a way we still want to surface something for).
        """
        try:
            raw = await self._run(stack_dir, "config")
            return yaml.safe_load(raw) or {}
        except ComposeCommandError as err:
            _LOGGER.warning(
                "docker compose config failed for %s, falling back to raw YAML read: %s",
                stack_dir,
                err,
            )
            return read_raw_compose_file(stack_dir)

    async def up(self, stack_dir: Path) -> None:
        """Start (or recreate) a stack. Works identically whether never
        started or previously stopped."""
        await self._run(stack_dir, "up", "-d", timeout=300.0)

    async def down(self, stack_dir: Path) -> None:
        """Stop a stack. Deliberately never passes -v: named volumes must
        never be removed as part of a normal stop/start cycle."""
        await self._run(stack_dir, "down", timeout=120.0)

    async def pull(self, stack_dir: Path, service: str | None = None) -> None:
        args = ["pull"]
        if service:
            args.append(service)
        await self._run(stack_dir, *args, timeout=600.0)

    async def restart(self, stack_dir: Path, service: str | None = None) -> None:
        args = ["restart"]
        if service:
            args.append(service)
        await self._run(stack_dir, *args, timeout=120.0)

    async def stop(self, stack_dir: Path, service: str | None = None) -> None:
        args = ["stop"]
        if service:
            args.append(service)
        await self._run(stack_dir, *args, timeout=60.0)

    async def start(self, stack_dir: Path, service: str | None = None) -> None:
        args = ["start"]
        if service:
            args.append(service)
        await self._run(stack_dir, *args, timeout=60.0)

    async def start_detached(self, stack_dir: Path, *args: str) -> DetachedExecHandle:
        """Like up()/pull()/etc., but starts the command as a DETACHED exec
        rather than blocking for its full duration — for commands that can
        recreate the very container HA itself runs in, where staying
        attached for the whole duration is unsafe (see
        DETACHED_EXEC_SPEC.md; pull_jobs.py is the caller). This method
        only starts the command — the caller is responsible for polling
        completion via poll() and checking the resulting exit code."""
        cmd = ["docker", "compose", *args]
        try:
            return await self._engine.exec_start_detached(
                self._sidecar_container, cmd, workdir=str(stack_dir)
            )
        except SidecarNotAvailableError as err:
            raise ComposeCommandError(list(args), -1, str(err)) from err

    async def poll(self, exec_id: str) -> ExecPollResult:
        """Check on a command started via start_detached() — safe to call
        from a freshly restarted HA process using only the exec ID (see
        engine.DockerEngineClient.poll_exec)."""
        return await self._engine.poll_exec(exec_id)

    async def read_log(self, log_path: str) -> str:
        """Best-effort retrieval of a detached command's captured output —
        see engine.DockerEngineClient.read_exec_log."""
        return await self._engine.read_exec_log(self._sidecar_container, log_path)


def read_raw_compose_file(stack_dir: Path) -> dict[str, Any]:
    """Fallback compose-config read, no `.env` substitution or Compose
    normalization — used only when the sidecar can't be reached at all
    (e.g. during config-flow validation, before a ComposeExecutor exists,
    or if get_compose_config's exec attempt fails)."""
    for filename in COMPOSE_FILENAMES:
        path = stack_dir / filename
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}
