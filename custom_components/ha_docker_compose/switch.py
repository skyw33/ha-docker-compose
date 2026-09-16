"""Stack-level and service-level start/stop switches.

Backed by the real `docker compose` CLI, exec'd into the sidecar container
(compose.py / ComposeExecutor), never the raw Engine API directly, so
Compose's own bookkeeping (dependency order, stop_grace_period, etc.) is
respected.

A service (or stack) marked `ha_docker_compose.protection: full` in its
compose labels gets no switch at all — see protection.py and
PROTECTED_STACK_SPEC.md. This is for genuinely self-referential
infrastructure (e.g. the sidecar the integration execs into to run every
compose command, including any command targeting itself) where a stop
issued by the very process being stopped is a real race condition with no
safe general fix, not a general-purpose safety feature.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .compose import ComposeCommandError, ComposeExecutor
from .const import DOMAIN, STACK_STATE_STOPPED
from .coordinator import StacksCoordinator
from .engine import ContainerInfo
from .entity import StackDeviceEntity, service_attributes, service_device_info
from .protection import is_service_protected, is_stack_protected

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: StacksCoordinator = data["coordinator"]
    compose_executor: ComposeExecutor = data["compose_executor"]

    entities: list[SwitchEntity] = []
    for stack in coordinator.stacks:
        if is_stack_protected(stack.compose_config):
            _LOGGER.debug(
                "Skipping switch.%s_running: stack has at least one protection=full service",
                stack.name,
            )
        else:
            entities.append(
                StackRunningSwitch(coordinator, compose_executor, entry.entry_id, stack.name)
            )

        services = stack.compose_config.get("services") or {}
        for service in stack.service_names:
            if is_service_protected(services.get(service, {})):
                _LOGGER.debug(
                    "Skipping switch.%s_%s_running: service is protection=full", stack.name, service
                )
                continue
            entities.append(
                ServiceRunningSwitch(
                    coordinator, compose_executor, entry.entry_id, stack.name, service
                )
            )

    async_add_entities(entities)


class StackRunningSwitch(StackDeviceEntity, SwitchEntity):
    _attr_icon = "mdi:layers-outline"

    def __init__(
        self,
        coordinator: StacksCoordinator,
        compose_executor: ComposeExecutor,
        entry_id: str,
        stack_name: str,
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._compose = compose_executor
        self._attr_unique_id = f"{entry_id}_{stack_name}_running"
        # Display name only — states the real mechanism (docker compose up
        # -d / docker compose down), not just "Running", since HA's device
        # page has no room for secondary text under a control. See
        # ENTITY_NAMES_SPEC.md. unique_id/entity_id are unchanged.
        self._attr_name = "Start/Stop (Compose Up/Down)"

    @property
    def is_on(self) -> bool | None:
        status = self._status
        if status is None:
            return None
        return status.state != STACK_STATE_STOPPED

    async def async_turn_on(self, **kwargs) -> None:
        status = self._status
        if status is None or status.info.path is None:
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")
        try:
            await self._compose.up(status.info.path)
        except ComposeCommandError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        status = self._status
        if status is None or status.info.path is None:
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")
        try:
            await self._compose.down(status.info.path)
        except ComposeCommandError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class ServiceRunningSwitch(StackDeviceEntity, SwitchEntity):
    _attr_icon = "mdi:docker"

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
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_running"
        # Display name only — see ENTITY_NAMES_SPEC.md. No parenthetical
        # here: this runs `docker compose start/stop <service>`, not
        # up/down (confirmed against compose.py before naming this), so
        # asserting "(Compose Up/Down)" the way the stack-level switch does
        # would have been actively wrong.
        self._attr_name = f"{service_name} Start/Stop"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _container(self) -> ContainerInfo | None:
        status = self._status
        if status is None:
            return None
        return status.container_for_service(self._service_name)

    @property
    def is_on(self) -> bool | None:
        container = self._container
        if container is None:
            return False
        return container.state == "running"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # See SERVICE_ATTRIBUTES_SPEC.md — plain, un-doubled stack/service
        # names, so dashboard code never needs to parse them back out of
        # entity_id. Always present (not gated behind coordinator data
        # availability): both values are known from construction, not
        # derived from a poll.
        return service_attributes(self._stack_name, self._service_name)

    async def async_turn_on(self, **kwargs) -> None:
        status = self._status
        if status is None or status.info.path is None:
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")
        try:
            await self._compose.start(status.info.path, self._service_name)
        except ComposeCommandError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        status = self._status
        if status is None or status.info.path is None:
            raise HomeAssistantError(f"Stack '{self._stack_name}' has no folder on disk")
        try:
            await self._compose.stop(status.info.path, self._service_name)
        except ComposeCommandError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
