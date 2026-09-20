"""Docker Engine API access via aiodocker.

Three distinct uses of the one shared connection:
- The live state / stats layer (container status, CPU/memory, uptime,
  health) — never for stack discovery, which is filesystem-only (see
  discovery.py). Containers are joined back to their stack via the
  `com.docker.compose.project` label.
- Attached Docker exec into a sidecar container to run short commands and
  capture their output directly (exec_in_container) — used by compose.py
  for commands that don't risk killing the connection watching them.
- Detached, poll-based exec (exec_start_detached / poll_exec /
  read_exec_log) — used for compose commands that can recreate the very
  container HA itself runs in (pull/up), where an attached connection is
  unsafe: see DETACHED_EXEC_SPEC.md for why "the client watching the
  command is itself killed by the command" is a real, confirmed failure
  mode, not a theoretical one.

Either exec style runs inside a sidecar container rather than as a local
subprocess of the Home Assistant process, since the stock HA image doesn't
have the `docker` CLI installed — see SIDECAR_EXEC_SPEC.md.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiodocker
from aiodocker.execs import Exec as AiodockerExec

from .const import COMPOSE_PROJECT_LABEL
from .image_ref import repo_name

_LOGGER = logging.getLogger(__name__)


class SidecarNotAvailableError(Exception):
    """The configured sidecar container couldn't be exec'd into at all —
    not found, not running, or the exec attempt otherwise failed/timed out.
    Distinct from a compose command that ran but exited non-zero, which is
    ComposeCommandError's job (see compose.py)."""


class ExecNotFoundError(Exception):
    """A previously-started exec ID is no longer known to the Docker
    daemon — e.g. the sidecar container itself was restarted, which drops
    all its in-flight exec state. Distinct from SidecarNotAvailableError:
    this means "the command's outcome is now unknowable," not "couldn't
    reach the sidecar at all." Callers (pull_jobs.py) should treat this as
    a genuine, rare, unrecoverable-for-that-job edge case — log it clearly
    rather than guessing at an outcome."""


class ContainerLogsUnavailableError(Exception):
    """Couldn't fetch a container's log output — container gone, daemon/
    proxy unreachable, or the request timed out. Deliberately distinct
    from SidecarNotAvailableError: this is about the *target* container
    whose logs were requested (e.g. jellyfin), not the sidecar — log
    retrieval is a plain Engine API read with no sidecar/exec involvement
    at all (see LOGS_BUTTON_SPEC.md)."""


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class DetachedExecHandle:
    """Handle to a detached exec — everything needed to poll it later,
    including from a freshly restarted HA process that has no memory of
    having started it."""

    exec_id: str
    log_path: str


@dataclass
class ExecPollResult:
    running: bool
    exit_code: int | None


COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


@dataclass
class ContainerInfo:
    """A single container's live state, joined back to its compose stack/service."""

    id: str
    name: str
    project: str | None
    service: str | None
    state: str  # running / exited / paused / restarting / created / dead
    health: str | None
    image: str
    image_id: str
    started_at: datetime | None
    cpu_percent: float | None
    memory_usage_bytes: int | None
    memory_limit_bytes: int | None
    # The container's own labels (compose-applied ones included) — already
    # fetched as part of the regular poll, so this costs no extra Engine
    # API call. Used as a fallback source for the GitHub-release OCI label
    # when the image itself doesn't set it (see github_coordinator.py).
    labels: dict[str, str] = field(default_factory=dict)
    # The host's CPU core count as this container's own stats sample
    # reported it (Docker's own `online_cpus`, already read internally by
    # _calculate_cpu_percent) — every container on one host reports the
    # same value. Exposed here purely as a fallback source for the
    # per-site CPU total's host-core-count normalization
    # (container_totals.resolve_host_cpu_cores) when the Engine API's
    # /info call is unavailable — see MULTI_SITE_IDENTITY_SPEC.md's
    # CPU-percentage-of-host amendment. None whenever cpu_percent is also
    # None (not running, or the stats read failed).
    online_cpus: int | None = None


class DockerEngineClient:
    """Thin async wrapper around aiodocker for the fast live-state poll.

    docker_host is a full Docker daemon address (aiodocker passes it
    straight through) — either `unix:///path/to/docker.sock` for a direct
    socket mount, or `tcp://host:port` to go through docker-socket-proxy
    (the default deployment — see SOCKET_PROXY_SPEC.md). Nothing here
    assumes one or the other; the transport is entirely a config value.
    """

    def __init__(self, docker_host: str) -> None:
        self._docker_host = docker_host
        self._docker: aiodocker.Docker | None = None

    async def connect(self) -> None:
        self._docker = aiodocker.Docker(url=self._docker_host)

    async def close(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None

    async def get_host_cpu_count(self) -> int | None:
        """The Docker host's CPU core count, from the Engine API's
        GET /info (`NCPU`) — see MULTI_SITE_IDENTITY_SPEC.md's
        CPU-percentage-of-host amendment. Chosen as the primary source
        over `online_cpus` (also available per-container in stats
        samples) because it's one cheap call that answers the question
        directly, independent of whether any container happens to be
        running yet — `online_cpus` only exists as a fallback for when
        this call itself fails (see container_totals.resolve_host_cpu_cores),
        not the other way around.

        None on any failure (proxy down, malformed response, missing/
        invalid NCPU) — callers must treat that as "unknown," never as
        0 or 1 cores, and fall back to ContainerInfo.online_cpus instead.
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")
        try:
            info = await self._docker.system.info()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to fetch Docker Engine /info for host CPU count", exc_info=True)
            return None
        ncpu = info.get("NCPU")
        if not isinstance(ncpu, int) or ncpu <= 0:
            _LOGGER.warning("Docker Engine /info returned an invalid NCPU value: %r", ncpu)
            return None
        return ncpu

    async def exec_in_container(
        self,
        container_name: str,
        cmd: list[str],
        workdir: str | None = None,
        timeout: float = 60.0,
    ) -> ExecResult:
        """Run cmd inside container_name via Docker exec (create + start +
        inspect) and return its captured output and exit code.

        Used to run `docker compose` inside a sidecar container rather than
        as a local subprocess of the HA process itself, so the stock HA
        image never needs the `docker` CLI installed — see
        SIDECAR_EXEC_SPEC.md. Raises SidecarNotAvailableError for anything
        that prevents getting a result at all (container missing/not
        running, exec API failure, timeout); a non-zero exit code from a
        command that *did* run is returned normally in ExecResult, not
        raised, since "the command failed" and "couldn't even run it" are
        different failure modes for callers to distinguish.
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")

        container = self._docker.containers.container(container_name)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _run() -> int:
            exec_ = await container.exec(cmd=cmd, stdout=True, stderr=True, workdir=workdir)
            async with exec_.start(detach=False) as stream:
                while True:
                    message = await stream.read_out()
                    if message is None:
                        break
                    (stderr_chunks if message.stream == 2 else stdout_chunks).append(message.data)
            info = await exec_.inspect()
            exit_code = info.get("ExitCode")
            return exit_code if exit_code is not None else -1

        try:
            exit_code = await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError as err:
            raise SidecarNotAvailableError(
                f"Exec into sidecar container '{container_name}' timed out after {timeout}s"
            ) from err
        except aiodocker.exceptions.DockerError as err:
            raise SidecarNotAvailableError(
                f"Could not exec into sidecar container '{container_name}' "
                f"(not found, or not running?): {err}"
            ) from err

        return ExecResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_chunks).decode(errors="replace"),
            stderr=b"".join(stderr_chunks).decode(errors="replace"),
        )

    async def exec_start_detached(
        self, container_name: str, cmd: list[str], workdir: str | None = None
    ) -> DetachedExecHandle:
        """Start cmd inside container_name as a DETACHED exec: the command
        keeps running to completion inside container_name independently of
        this connection, or even of this HA process staying alive for its
        duration — unlike exec_in_container(), where the command and the
        connection watching it share a fate. See DETACHED_EXEC_SPEC.md.

        A detached exec has no attached stdout/stderr to capture directly,
        so output is redirected to a log file inside container_name;
        retrieve it later with read_exec_log() (best-effort — the log is a
        diagnostic nicety, never required to know whether the command
        succeeded, which poll_exec()'s exit code alone already answers).
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")

        log_path = f"/tmp/ha_docker_compose_exec_{uuid.uuid4().hex}.log"
        wrapped_cmd = ["sh", "-c", f"{shlex.join(cmd)} > {shlex.quote(log_path)} 2>&1"]

        container = self._docker.containers.container(container_name)
        try:
            exec_ = await container.exec(cmd=wrapped_cmd, stdout=True, stderr=True, workdir=workdir)
            await exec_.start(detach=True)
        except aiodocker.exceptions.DockerError as err:
            raise SidecarNotAvailableError(
                f"Could not start detached exec in sidecar container '{container_name}': {err}"
            ) from err

        return DetachedExecHandle(exec_id=exec_.id, log_path=log_path)

    async def poll_exec(self, exec_id: str) -> ExecPollResult:
        """Check on a previously-started exec by ID alone — no dependency
        on the connection (or even the HA process) that originally started
        it, safe to call fresh after a restart using only a persisted ID.

        Raises ExecNotFoundError only for a genuine 404 (the daemon no
        longer knows this exec ID at all — e.g. the sidecar container
        itself was restarted, dropping its exec state): a real,
        "outcome-unknowable" case for callers to surface honestly. Any
        other Docker/network error (a transient proxy blip, timeout, etc.)
        raises SidecarNotAvailableError instead — that's "couldn't check
        right now," not "the command is gone," and callers should retry
        rather than give up on the job.
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")

        exec_ = AiodockerExec(self._docker, exec_id)
        try:
            info = await exec_.inspect()
        except aiodocker.exceptions.DockerError as err:
            if err.status == 404:
                raise ExecNotFoundError(f"Exec '{exec_id}' is no longer known to Docker: {err}") from err
            raise SidecarNotAvailableError(f"Could not poll exec '{exec_id}': {err}") from err

        return ExecPollResult(running=bool(info.get("Running")), exit_code=info.get("ExitCode"))

    async def read_exec_log(self, container_name: str, log_path: str) -> str:
        """Best-effort retrieval (and cleanup) of a detached exec's
        redirected output. Failures here are non-fatal — the caller already
        knows success/failure from poll_exec()'s exit code; this is only
        for diagnostic detail in logs/error messages."""
        try:
            result = await self.exec_in_container(container_name, ["cat", log_path], timeout=15.0)
            await self.exec_in_container(container_name, ["rm", "-f", log_path], timeout=15.0)
            return result.stdout
        except Exception as err:  # noqa: BLE001 - diagnostic-only, must not raise
            _LOGGER.debug("Failed to read/clean up exec log %s in %s: %s", log_path, container_name, err)
            return ""

    async def _inspect_image(self, image_ref: str) -> dict[str, Any] | None:
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")
        try:
            return await self._docker.images.inspect(image_ref)
        except aiodocker.exceptions.DockerError as err:
            _LOGGER.debug("Local image inspect failed for %s: %s", image_ref, err)
            return None

    async def get_image_repo_digest(self, image_ref: str) -> str | None:
        """Return the manifest digest (sha256:...) of the locally pulled
        image matching image_ref, read off RepoDigests rather than the
        image ID — RepoDigests is what's directly comparable to the
        registry client's remote manifest digest (registry_client.py),
        since both describe the same registry manifest rather than the
        local unpacked image config."""
        details = await self._inspect_image(image_ref)
        if details is None:
            _LOGGER.debug(
                "get_image_repo_digest(%s): image not found locally, returning None", image_ref
            )
            return None

        target_repo = repo_name(image_ref)
        repo_digests = details.get("RepoDigests") or []
        for repo_digest in repo_digests:
            name, _, digest = repo_digest.partition("@")
            if name == target_repo and digest:
                return digest

        _LOGGER.debug(
            "get_image_repo_digest(%s): no RepoDigest matched repo '%s' (RepoDigests on the "
            "local image: %s) — returning None",
            image_ref,
            target_repo,
            repo_digests,
        )
        return None

    async def get_image_labels(self, image_ref: str) -> dict[str, str]:
        """Return the local image's baked-in labels (e.g. OCI annotations
        like org.opencontainers.image.source), used for the best-effort
        GitHub-release lookup — never for update-available, which is
        digest-only."""
        details = await self._inspect_image(image_ref)
        if details is None:
            return {}
        return (details.get("Config", {}) or {}).get("Labels") or {}

    async def get_container_logs(self, container_name: str, tail: int = 100) -> list[str]:
        """Fetch the last `tail` lines of a container's combined
        stdout+stderr log output — a plain, one-shot Engine API read
        (`follow=False`), no exec/sidecar involvement at all: unlike the
        compose command layer, log retrieval doesn't require running
        anything inside the container, just reading its existing log
        buffer, which Docker retains for stopped containers too. See
        LOGS_BUTTON_SPEC.md.
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")

        container = self._docker.containers.container(container_name)
        try:
            lines = await asyncio.wait_for(
                container.log(stdout=True, stderr=True, follow=False, tail=str(tail)),
                timeout=30.0,
            )
        except asyncio.TimeoutError as err:
            raise ContainerLogsUnavailableError(
                f"Timed out fetching logs for container '{container_name}'"
            ) from err
        except aiodocker.exceptions.DockerError as err:
            raise ContainerLogsUnavailableError(
                f"Could not fetch logs for container '{container_name}': {err}"
            ) from err

        return list(lines)

    async def list_containers(self) -> list[ContainerInfo]:
        """Return live info for every container (running or not), across all
        compose projects and including non-compose containers (project is
        then None) so orphan detection has the full picture.

        Every container's inspect+stats calls run concurrently, not one
        after another. A one-shot `stats(stream=False)` call is inherently
        slow on the Engine API — it samples CPU usage twice internally to
        compute a delta, even for a single snapshot, so it costs roughly a
        second regardless — and awaiting that sequentially per container
        made this poll scale linearly with container count (tens of
        seconds once there were several dozen), rather than the small,
        roughly-constant wall time gathering them concurrently gives.
        """
        if self._docker is None:
            raise RuntimeError("DockerEngineClient.connect() must be called first")

        containers = await self._docker.containers.list(all=True)

        inspected = await asyncio.gather(
            *(self._inspect_one(container) for container in containers),
            return_exceptions=True,
        )

        results: list[ContainerInfo] = []
        for container, outcome in zip(containers, inspected):
            if isinstance(outcome, BaseException):
                _LOGGER.error(
                    "Failed to inspect container %s",
                    getattr(container, "id", "?"),
                    exc_info=outcome,
                )
                continue
            results.append(outcome)

        return results

    async def _inspect_one(self, container: Any) -> ContainerInfo:
        details = await container.show()
        labels = (details.get("Config", {}) or {}).get("Labels") or {}
        state = details.get("State", {}) or {}

        cpu_percent: float | None = None
        online_cpus: int | None = None
        memory_usage: int | None = None
        memory_limit: int | None = None
        if state.get("Running"):
            try:
                stats = await container.stats(stream=False)
                sample = stats[0] if isinstance(stats, list) else stats
                cpu_percent, online_cpus = _calculate_cpu_percent(sample)
                mem = sample.get("memory_stats", {}) or {}
                memory_usage = mem.get("usage")
                memory_limit = mem.get("limit")
                if memory_usage is None or memory_limit is None:
                    # Known cgroup v1/v2 stats-translation inconsistency on
                    # the DAEMON side, not something this code reads from
                    # /sys/fs/cgroup itself — we only ever consume the
                    # Engine API's already-built JSON. Log the raw shape so
                    # a real occurrence is diagnosable instead of just
                    # silently showing Unknown.
                    _LOGGER.debug(
                        "Container %s: memory_stats missing 'usage'/'limit' "
                        "(usage=%r, limit=%r) — raw memory_stats from the Engine API: %r",
                        details.get("Id"),
                        memory_usage,
                        memory_limit,
                        mem,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to fetch stats for container %s", details.get("Id"))

        started_at = None
        started_at_raw = state.get("StartedAt")
        if started_at_raw and not started_at_raw.startswith("0001-01-01"):
            try:
                started_at = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            except ValueError:
                started_at = None

        return ContainerInfo(
            id=details["Id"],
            name=(details.get("Name") or "").lstrip("/"),
            project=labels.get(COMPOSE_PROJECT_LABEL),
            service=labels.get(COMPOSE_SERVICE_LABEL),
            state=state.get("Status", "unknown"),
            health=(state.get("Health") or {}).get("Status"),
            image=details.get("Config", {}).get("Image", ""),
            # The container's "Image" field is the local image ID
            # (sha256:...), a diagnostic value distinct from the manifest
            # digest used for update comparison — see
            # get_image_repo_digest(), which looks up RepoDigests instead.
            image_id=details.get("Image", ""),
            started_at=started_at,
            cpu_percent=cpu_percent,
            online_cpus=online_cpus,
            memory_usage_bytes=memory_usage,
            memory_limit_bytes=memory_limit,
            labels=labels,
        )


def _calculate_cpu_percent(stats: dict[str, Any]) -> tuple[float | None, int | None]:
    """Standard Docker CPU% formula (the same one `docker stats` uses).

    Returns (cpu_percent, online_cpus) — online_cpus is returned too, not
    just used internally, since it doubles as ContainerInfo.online_cpus,
    the fallback host-core-count source for the per-site CPU total when
    the Engine API's /info call is unavailable (see
    DockerEngineClient.get_host_cpu_count() and container_totals.py).
    """
    online_cpus: int | None = None
    try:
        cpu = stats["cpu_stats"]
        online_cpus = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        precpu = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - precpu["cpu_usage"]["total_usage"]
        system_delta = cpu["system_cpu_usage"] - precpu["system_cpu_usage"]
        if system_delta > 0 and cpu_delta >= 0:
            return (cpu_delta / system_delta) * online_cpus * 100.0, online_cpus
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return None, online_cpus
