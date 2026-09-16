"""Shared device-grouping helpers.

Every stack gets one HA device; every service within a stack gets its own
*child* device (linked to the stack device via `via_device`), rather than
sharing the stack's device the way it used to. This is what makes
per-service cleanup possible on reload: __init__.py's stale-device pruning
removes a device (and, via Home Assistant's own cascade, every entity
attached to it) whenever its identifier isn't in the freshly discovered
set (device_ids.expected_device_identifiers). Keeping services on their
own device means a service dropped from an edited compose file can be
pruned without touching the rest of its still-valid stack.

NOTE: `via_device` (identifier-tuple form) is used deliberately here, not
`via_device_id`. An earlier attempt to "fix" a via_device deprecation
warning by swapping to `via_device_id=<identifier tuple>` caused every
service-level entity across every stack to stop being re-registered on
setup (HA's "no longer provided by this integration" state) — via_device_id
most likely expects an already-resolved device registry ID string, not an
identifier tuple, so the type mismatch broke device registration entirely.
Do not repeat that swap without first confirming the exact expected value
type from a real, current HA source tree or changelog — verify against
actual HA source, not by inference, before touching this again.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import StacksCoordinator, StackStatus
from .device_ids import service_device_identifier, stack_device_identifier


def stack_device_info(entry_id: str, stack_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={stack_device_identifier(entry_id, stack_name)},
        name=stack_name,
        manufacturer="Docker Compose",
    )


def service_device_info(entry_id: str, stack_name: str, service_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={service_device_identifier(entry_id, stack_name, service_name)},
        name=f"{stack_name} / {service_name}",
        manufacturer="Docker Compose",
        via_device=stack_device_identifier(entry_id, stack_name),
    )


def service_attributes(stack_name: str, service_name: str) -> dict[str, str]:
    """The `stack`/`service` attribute pair every per-service entity
    exposes — plain, un-doubled values, so dashboard code never needs to
    reverse-engineer either by parsing entity_id. See
    SERVICE_ATTRIBUTES_SPEC.md: entity_id-based parsing is fragile
    (several real stack names contain underscores) and at least one real
    string-prefix collision exists (music_assistant vs.
    music_assistant_backup) — a plain literal attribute removes the need
    for any client-side parsing/guessing at all. One canonical place for
    the two key names, used consistently by every per-service entity
    across sensor.py/switch.py/button.py/binary_sensor.py, rather than
    the key names being repeated (and risking drift) at every call site.
    """
    return {"stack": stack_name, "service": service_name}


class StackDeviceEntity(CoordinatorEntity[StacksCoordinator]):
    """Base for a stack-level entity, attached to the stack's own device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StacksCoordinator, entry_id: str, stack_name: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._stack_name = stack_name
        self._attr_device_info = stack_device_info(entry_id, stack_name)

    @property
    def _status(self) -> StackStatus | None:
        return (self.coordinator.data or {}).get(self._stack_name)
