"""update_available binary sensors — digest comparison only.

Deliberately not linked to "what's the latest released version": this only
ever answers "has the digest behind this tag changed" (see
PROJECT_SPEC.md's explicit non-goal on version-string resolution).

Two levels:
- `binary_sensor.{stack}_{service}_update_available` — per-service, as
  before.
- `binary_sensor.{stack}_update_available` — a cheap, no-new-I/O
  stack-level OR aggregation (see update_coordinator.py's
  StackUpdateSummary), so a stacks-overview dashboard card doesn't need to
  scan the full entity registry per card/render to answer "does this stack
  have anything pending."
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StacksCoordinator
from .entity import service_attributes, service_device_info, stack_attributes, stack_device_info
from .update_coordinator import ServiceUpdateStatus, UpdateCheckCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    stacks_coordinator: StacksCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    update_coordinator: UpdateCheckCoordinator = hass.data[DOMAIN][entry.entry_id][
        "update_coordinator"
    ]

    entities: list[BinarySensorEntity] = []
    for stack in stacks_coordinator.stacks:
        entities.append(
            StackUpdateAvailableSensor(
                update_coordinator, entry.entry_id, stack.name, stacks_coordinator.site
            )
        )
        for service in stack.service_names:
            entities.append(
                ServiceUpdateAvailableSensor(update_coordinator, entry.entry_id, stack.name, service)
            )

    async_add_entities(entities)


class ServiceUpdateAvailableSensor(CoordinatorEntity[UpdateCheckCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:package-up"

    def __init__(
        self,
        coordinator: UpdateCheckCoordinator,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._service_name = service_name
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_update_available"
        self._attr_name = f"{service_name} update available"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _status(self) -> ServiceUpdateStatus | None:
        summary = (self.coordinator.data or {}).get(self._stack_name)
        return summary.services.get(self._service_name) if summary else None

    @property
    def available(self) -> bool:
        """Standing conditions (unsupported registry, auth denied) show as
        genuinely unavailable, not just "unknown" — they won't resolve on
        their own next poll the way a transient network error might."""
        status = self._status
        if not super().available or status is None:
            return False
        return status.registry_supported and not status.auth_denied

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.update_available if status else None

    @property
    def extra_state_attributes(self) -> dict:
        # stack/service (SERVICE_ATTRIBUTES_SPEC.md) are always present —
        # known from construction, not derived from coordinator data —
        # even when there's no status yet to report the rest.
        attrs = service_attributes(self._stack_name, self._service_name)
        status = self._status
        if status is None:
            return attrs
        attrs.update(
            {
                "image": status.image,
                "local_digest": status.local_digest,
                "remote_digest": status.remote_digest,
                "registry_supported": status.registry_supported,
                "auth_denied": status.auth_denied,
            }
        )
        return attrs


class StackUpdateAvailableSensor(CoordinatorEntity[UpdateCheckCoordinator], BinarySensorEntity):
    """OR aggregation across every service in the stack (see
    update_coordinator.py's StackUpdateSummary.update_available) — on if
    ANY service has a confirmed pending update. Pure in-memory aggregation
    over data this coordinator already collects each cycle; no new Engine
    API calls, no new registry calls, no new polling."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:package-up"

    def __init__(
        self, coordinator: UpdateCheckCoordinator, entry_id: str, stack_name: str, site: str
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._site = site
        self._attr_unique_id = f"{entry_id}_{stack_name}_update_available"
        self._attr_name = "Update available"
        self._attr_device_info = stack_device_info(entry_id, stack_name, site)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return stack_attributes(self._site, self._stack_name)

    @property
    def is_on(self) -> bool | None:
        summary = (self.coordinator.data or {}).get(self._stack_name)
        return summary.update_available if summary else None
