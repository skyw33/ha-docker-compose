"""Stack-level pull+recreate/check-updates and service-level
restart/fetch-logs buttons.

A service (or stack) marked `ha_docker_compose.protection: full` in its
compose labels gets no restart/pull button at all — `check_updates` and
`fetch_logs` are unaffected (neither touches a container: check_updates
only re-runs the digest check, fetch_logs only reads the container's
existing log buffer via the Engine API). See protection.py and
PROTECTED_STACK_SPEC.md.

Pull update runs via PullJobRunner (pull_jobs.py) — a detached, polled,
restart-resumable exec — rather than an attached exec awaited inline here,
since `docker compose up -d` can recreate the very container HA itself
runs in. See DETACHED_EXEC_SPEC.md. Restart/stop/start still use the
simpler attached exec (compose.py's ComposeExecutor._run): scoped out of
this pass per DETACHED_EXEC_SPEC.md's "worth Claude Code's judgment"
allowance — pull/up are the commands proven to trigger the
kills-its-own-watcher scenario, and extending the same
persist-and-resume treatment to restart/stop/start would roughly double
this change's size for a much rarer failure window (those commands don't
routinely recreate a container the way pull+up does). Worth a follow-up
if real-world use shows it's needed.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .compose import ComposeCommandError, ComposeExecutor
from .const import DOMAIN, LOG_FETCH_TAIL_LINES
from .coordinator import LogFetchResult, StacksCoordinator
from .engine import ContainerLogsUnavailableError, DockerEngineClient
from .entity import StackDeviceEntity, service_attributes, service_device_info
from .protection import is_service_protected, is_stack_protected
from .pull_jobs import PullJobRunner
from .tag_walk_coordinator import TagWalkCoordinator
from .update_coordinator import UpdateCheckCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: StacksCoordinator = data["coordinator"]
    update_coordinator: UpdateCheckCoordinator = data["update_coordinator"]
    tag_walk_coordinator: TagWalkCoordinator = data["tag_walk_coordinator"]
    compose_executor: ComposeExecutor = data["compose_executor"]
    pull_job_runner: PullJobRunner = data["pull_job_runner"]
    engine: DockerEngineClient = data["engine"]

    entities: list[ButtonEntity] = []
    for stack in coordinator.stacks:
        if is_stack_protected(stack.compose_config):
            _LOGGER.debug(
                "Skipping button.%s_pull_update: stack has at least one protection=full service",
                stack.name,
            )
        else:
            entities.append(
                StackPullUpdateButton(coordinator, pull_job_runner, entry.entry_id, stack.name)
            )

        entities.append(
            StackCheckUpdatesButton(
                coordinator, update_coordinator, tag_walk_coordinator, entry.entry_id, stack.name
            )
        )

        services = stack.compose_config.get("services") or {}
        for service in stack.service_names:
            entities.append(
                ServiceFetchLogsButton(coordinator, engine, entry.entry_id, stack.name, service)
            )

            if is_service_protected(services.get(service, {})):
                _LOGGER.debug(
                    "Skipping button.%s_%s_restart: service is protection=full", stack.name, service
                )
                continue
            entities.append(
                ServiceRestartButton(
                    coordinator, compose_executor, entry.entry_id, stack.name, service
                )
            )

    async_add_entities(entities)


class StackPullUpdateButton(StackDeviceEntity, ButtonEntity):
    """Pulls the latest images for every service in the stack and recreates
    it (`docker compose pull` + `docker compose up -d`), via a detached,
    restart-resumable exec (see pull_jobs.PullJobRunner) rather than
    blocking this call for the whole duration. Digest-history recording,
    the update-check auto-refresh, and clearing the transient "updating"
    state on sensor.{stack}_state all happen once the job is confirmed
    complete — which may be well after this method itself returns, since
    the job outlives an HA restart if one happens mid-pull."""

    _attr_icon = "mdi:cloud-download-outline"

    def __init__(
        self,
        coordinator: StacksCoordinator,
        pull_job_runner: PullJobRunner,
        entry_id: str,
        stack_name: str,
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._pull_job_runner = pull_job_runner
        self._attr_unique_id = f"{entry_id}_{stack_name}_pull_update"
        # Display name only — see ENTITY_NAMES_SPEC.md. Names the real
        # subcommands: `docker compose pull` then `docker compose up -d`
        # (no `down` first — `up -d` recreates only what changed).
        # unique_id/entity_id are unchanged.
        self._attr_name = "Update (Compose Pull & Up)"

    async def async_press(self) -> None:
        # Unconditional, before anything else can short-circuit: if this
        # line never shows up in the log, the press isn't reaching this
        # method at all (stale deployed code is the usual reason — editing
        # a custom component's .py files needs a full HA restart to be
        # picked up; a config-entry Reload alone re-runs async_setup_entry
        # against the already-imported module, not a fresh one).
        _LOGGER.info("StackPullUpdateButton.async_press() called for stack '%s'", self._stack_name)

        status = self._status
        if status is None or status.info.path is None:
            _LOGGER.warning(
                "Pull update for stack '%s': no folder on disk (or stack missing from "
                "coordinator data) — aborting before running any compose command",
                self._stack_name,
            )
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")

        try:
            await self._pull_job_runner.start(self._stack_name, status.info.path)
        except ComposeCommandError as err:
            _LOGGER.warning("Pull update for stack '%s' failed to start: %s", self._stack_name, err)
            raise HomeAssistantError(str(err)) from err


class StackCheckUpdatesButton(StackDeviceEntity, ButtonEntity):
    """Manually triggers both a digest check AND a full tag-list refresh.
    Triggers a full refresh of both coordinators (all stacks), not just
    this one — neither coordinator supports a per-stack partial refresh,
    and a slightly wider check than strictly necessary is an acceptable
    v1 simplification here.

    Forcing the tag-walk coordinator too (not just the digest check) is a
    deliberate choice, not an oversight: this button exists specifically
    for "I want fresh data right now," and latest_registry_tag/
    pull_target_version are exactly the kind of data a manual press
    should bypass the (much longer, 18h) staleness window for — see
    REGISTRY_TAG_WALK_SPEC.md's "Prerequisite" amendment. This does mean
    a press now costs up to ~29 registry requests for a high-tag-count
    repository (ESPHome), not just the 1-2 the digest check alone needs —
    an accepted, deliberate cost for an explicitly on-demand action, not
    something that happens automatically on any regular cadence."""

    _attr_icon = "mdi:refresh"

    def __init__(
        self,
        coordinator: StacksCoordinator,
        update_coordinator: UpdateCheckCoordinator,
        tag_walk_coordinator: TagWalkCoordinator,
        entry_id: str,
        stack_name: str,
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._update_coordinator = update_coordinator
        self._tag_walk_coordinator = tag_walk_coordinator
        self._attr_unique_id = f"{entry_id}_{stack_name}_check_updates"
        # Display name only — see ENTITY_NAMES_SPEC.md. "(Registry)", not a
        # Compose command name: this makes no docker/docker compose call
        # at all, it's a direct HTTP call to the image registry's API
        # comparing digests. unique_id/entity_id are unchanged.
        self._attr_name = "Check for Update (Registry)"

    async def async_press(self) -> None:
        _LOGGER.info("StackCheckUpdatesButton.async_press() called for stack '%s'", self._stack_name)
        await self._update_coordinator.async_request_refresh()
        await self._tag_walk_coordinator.async_request_refresh()


class ServiceFetchLogsButton(StackDeviceEntity, ButtonEntity):
    """Fetches the last LOG_FETCH_TAIL_LINES lines of this service's
    container log output and writes them into
    sensor.{stack}_{service}_last_fetched_logs (state = fetch timestamp,
    `log_text` attribute = the content) — a quick "what just happened"
    snapshot, not live tailing. See FETCH_LOGS_ATTRIBUTE_SPEC.md, which
    supersedes the original LOGS_BUTTON_SPEC.md's `_LOGGER.info` output:
    HA's own Settings → System → Logs page only surfaces WARNING and
    above by default, so INFO-level output was never actually reachable
    from HA's UI. A plain Engine API read (engine.py's
    get_container_logs) — no exec/sidecar involvement, so this works
    identically whether the container is running or stopped (Docker
    retains log output for stopped containers), and doesn't touch the
    compose-command layer at all.

    Each press overwrites the sensor's previous snapshot in place — see
    coordinator.py's LogFetchResult/last_fetched_logs. The existing
    sensor.{stack}_{service}_log_command stays the right tool for active,
    ongoing `-f` debugging; this is for a quick look without leaving HA."""

    _attr_icon = "mdi:text-box-search-outline"

    def __init__(
        self,
        coordinator: StacksCoordinator,
        engine: DockerEngineClient,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._engine = engine
        self._service_name = service_name
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_fetch_logs"
        self._attr_name = f"{service_name} fetch logs"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # See SERVICE_ATTRIBUTES_SPEC.md.
        return service_attributes(self._stack_name, self._service_name)

    async def async_press(self) -> None:
        _LOGGER.info(
            "ServiceFetchLogsButton.async_press() called for stack '%s', service '%s'",
            self._stack_name,
            self._service_name,
        )
        status = self._status
        container = status.container_for_service(self._service_name) if status else None
        if container is None:
            _LOGGER.warning(
                "Fetch logs for stack '%s', service '%s': no known container for this service "
                "(never started?) — nothing to fetch",
                self._stack_name,
                self._service_name,
            )
            raise HomeAssistantError(
                f"No container found for '{self._stack_name}/{self._service_name}'"
            )

        try:
            lines = await self._engine.get_container_logs(
                container.name, tail=LOG_FETCH_TAIL_LINES
            )
        except ContainerLogsUnavailableError as err:
            _LOGGER.warning(
                "Fetch logs for stack '%s', service '%s' failed: %s",
                self._stack_name,
                self._service_name,
                err,
            )
            raise HomeAssistantError(str(err)) from err

        log_text = "\n".join(line.rstrip("\n") for line in lines) if lines else "(no log output)"
        # Overwrites any previous snapshot for this (stack, service) —
        # deliberately not appended to, see the class docstring.
        self.coordinator.last_fetched_logs[(self._stack_name, self._service_name)] = (
            LogFetchResult(fetched_at=dt_util.utcnow(), log_text=log_text)
        )
        self.coordinator.async_update_listeners()


class ServiceRestartButton(StackDeviceEntity, ButtonEntity):
    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: StacksCoordinator,
        compose_executor: ComposeExecutor,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._compose = compose_executor
        self._service_name = service_name
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_restart"
        # Display name only — see ENTITY_NAMES_SPEC.md. No parenthetical
        # (kept short, per spec) — runs `docker compose restart <service>`.
        self._attr_name = f"{service_name} Restart"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # See SERVICE_ATTRIBUTES_SPEC.md.
        return service_attributes(self._stack_name, self._service_name)

    async def async_press(self) -> None:
        _LOGGER.info(
            "ServiceRestartButton.async_press() called for stack '%s', service '%s'",
            self._stack_name,
            self._service_name,
        )
        status = self._status
        if status is None or status.info.path is None:
            _LOGGER.warning(
                "Restart for stack '%s', service '%s': no folder on disk (or stack missing from "
                "coordinator data) — aborting before running any compose command",
                self._stack_name,
                self._service_name,
            )
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")
        try:
            await self._compose.restart(status.info.path, self._service_name)
        except ComposeCommandError as err:
            _LOGGER.warning(
                "Restart for stack '%s', service '%s' failed: %s",
                self._stack_name,
                self._service_name,
                err,
            )
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
