"""Fast-interval coordinator: live container state + stats via aiodocker,
joined against the discovered (mostly static) stack list to derive stack
state.

Stack discovery (what stacks exist on disk) is intentionally NOT redone here
on every poll — compose files change rarely and re-running `docker compose
config` per stack per tick would be wasteful. Discovery happens once at
config entry setup; this coordinator only re-derives *state* from currently
running containers, which is the genuinely fast-changing part.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    STACK_STATE_ORPHANED,
    STACK_STATE_PARTIAL,
    STACK_STATE_RUNNING,
    STACK_STATE_STOPPED,
)
from .discovery import StackInfo
from .engine import ContainerInfo, DockerEngineClient

_LOGGER = logging.getLogger(__name__)

# Out-of-the-box default only — const.DEFAULT_POLL_INTERVAL_SECONDS is the
# single source of truth (also used as config_flow.py's schema default).
# Per-entry configurable poll interval (CONF_POLL_INTERVAL) overrides this
# via the update_interval constructor parameter below — see __init__.py.
FAST_POLL_INTERVAL = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS)


@dataclass
class StackStatus:
    """Derived state for one stack, plus the live containers behind it."""

    info: StackInfo
    state: str
    containers: list[ContainerInfo] = field(default_factory=list)

    def container_for_service(self, service: str) -> ContainerInfo | None:
        for container in self.containers:
            if container.service == service:
                return container
        return None


@dataclass
class PullError:
    """Most recent Pull update failure for a stack, if any — see
    PULL_ERROR_VISIBILITY_SPEC.md. Before this, a failure that occurred
    after PullJobRunner.start() returned (i.e. everything from
    _drive()/_wait_for_step() onward — a failed pull/up command, a lost
    exec, a sidecar timeout) was only ever logged, with the dashboard
    silently reverting from the transient "updating" state back to the
    stale prior state — no different, from the user's perspective, than
    success. Cleared at the start of a fresh Pull press (an old failure
    shouldn't linger once the user retries) and set by PullJobRunner
    wherever it currently logs a WARNING/ERROR and ends the job any way
    other than success."""

    failed_at: datetime
    step: str
    reason: str


@dataclass
class LogFetchResult:
    """One service's most recent Fetch logs snapshot — see
    FETCH_LOGS_ATTRIBUTE_SPEC.md. Overwritten in place on every button
    press, not appended to: a point-in-time snapshot, not a history."""

    fetched_at: datetime
    log_text: str


class StacksCoordinator(DataUpdateCoordinator[dict[str, "StackStatus"]]):
    """Polls live container state and derives per-stack/service state."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: DockerEngineClient,
        stacks: list[StackInfo],
        label: str,
        update_interval: timedelta = FAST_POLL_INTERVAL,
        *,
        site: str,
        cpu_cores: int | None = None,
    ) -> None:
        # label distinguishes this coordinator's log lines from another
        # config entry's (e.g. a second Docker host) — every entry's
        # coordinators otherwise shared this same static name, making
        # "Finished fetching ha_docker_compose_stacks data..." lines
        # indistinguishable across entries when running more than one.
        # update_interval is per-entry configurable (CONF_POLL_INTERVAL,
        # see __init__.py) — the default here only applies if a caller
        # doesn't pass one explicitly (e.g. an old test).
        super().__init__(
            hass,
            _LOGGER,
            name=f"ha_docker_compose_stacks ({label})",
            update_interval=update_interval,
        )
        self._engine = engine
        self.stacks = stacks
        # This entry's resolved, slugified site name — see
        # MULTI_SITE_IDENTITY_SPEC.md. Resolved once in __init__.py
        # (config-flow-chosen, or auto-derived and persisted to
        # entry.options on first load) and never recomputed here; read by
        # entity.py's stack_attributes() helper and by the per-entry total
        # sensors.
        self.site = site
        # Host CPU core count (Docker Engine API's /info NCPU), fetched
        # once per entry load by __init__.py and refreshed on every reload
        # (a fresh StacksCoordinator is constructed on each) — see
        # MULTI_SITE_IDENTITY_SPEC.md's CPU-percentage-of-host amendment.
        # None means the /info call failed at load time; sensor.py's
        # TotalContainerCpuSensor then falls back to ContainerInfo.
        # online_cpus from live stats samples instead (see
        # container_totals.resolve_host_cpu_cores) rather than treating
        # this as a hard, unrecoverable failure.
        self.cpu_cores = cpu_cores
        # Stack names with a Pull update in progress right now — set/cleared
        # by StackPullUpdateButton, read by StackStateSensor to show a
        # transient "updating" value in place of the real (momentarily
        # stale) container-derived state. Deliberately not part of `.data`:
        # this is a live-UI concern, not real observed state, and must
        # survive being read across multiple _async_update_data() cycles
        # without _derive_stack_state ever overwriting it — see sensor.py.
        # In-memory only, never persisted; a restart naturally clears it.
        self.pulling_stacks: set[str] = set()
        # Most recent Pull update failure per stack, if any — set/cleared by
        # PullJobRunner (pull_jobs.py), read by StackStateSensor. Same
        # live-UI-only rationale as pulling_stacks above: not part of
        # `.data`, not persisted, a restart naturally clears it. See
        # PULL_ERROR_VISIBILITY_SPEC.md.
        self.last_pull_errors: dict[str, PullError] = {}
        # Most recent Fetch logs result per (stack, service) — set by
        # ServiceFetchLogsButton (button.py), read by
        # ServiceLastFetchedLogsSensor (sensor.py). Same live-UI-only
        # rationale as pulling_stacks above: not part of `.data`, not
        # persisted, a restart naturally clears it, and nothing here ever
        # overwrites an entry except another button press for that same
        # service. See FETCH_LOGS_ATTRIBUTE_SPEC.md.
        self.last_fetched_logs: dict[tuple[str, str], LogFetchResult] = {}

    async def _async_update_data(self) -> dict[str, StackStatus]:
        try:
            containers = await self._engine.list_containers()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Failed to poll Docker engine: {err}") from err

        by_project: dict[str, list[ContainerInfo]] = {}
        for container in containers:
            if container.project:
                by_project.setdefault(container.project, []).append(container)

        result: dict[str, StackStatus] = {}

        for stack in self.stacks:
            stack_containers = by_project.pop(stack.name, [])
            result[stack.name] = StackStatus(
                info=stack,
                state=_derive_stack_state(stack, stack_containers),
                containers=stack_containers,
            )

        # Whatever is left in by_project has a compose project label but no
        # matching discovered folder: orphaned.
        for project, orphan_containers in by_project.items():
            result[project] = StackStatus(
                info=StackInfo(
                    name=project, path=None, compose_filename="", has_env_file=False
                ),
                state=STACK_STATE_ORPHANED,
                containers=orphan_containers,
            )

        return result


def _derive_stack_state(stack: StackInfo, containers: list[ContainerInfo]) -> str:
    if not containers:
        return STACK_STATE_STOPPED

    expected_services = set(stack.service_names) or {c.service for c in containers if c.service}
    running_services = {c.service for c in containers if c.service and c.state == "running"}

    if expected_services and running_services >= expected_services:
        return STACK_STATE_RUNNING
    if running_services:
        return STACK_STATE_PARTIAL
    return STACK_STATE_STOPPED
