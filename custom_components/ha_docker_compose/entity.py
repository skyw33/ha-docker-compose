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


def stack_device_info(entry_id: str, stack_name: str, site: str) -> DeviceInfo:
    # site in the display name, e.g. "jellyfin (nas)" — see
    # MULTI_SITE_IDENTITY_SPEC.md: two stacks with the same name on
    # different sites otherwise show up identically in HA's device list
    # with nothing to tell them apart, even though their identifiers
    # (entry_id-qualified — see device_ids.py) never actually collide.
    return DeviceInfo(
        identifiers={stack_device_identifier(entry_id, stack_name)},
        name=f"{stack_name} ({site})",
        manufacturer="Docker Compose",
    )


def service_device_info(
    entry_id: str, stack_name: str, service_name: str, site: str
) -> DeviceInfo:
    # site in the display name is required for uniqueness, not cosmetic:
    # service entity_ids derive from the service device's own name (see
    # stack_device_info's identical reasoning) — without it, two
    # same-named stacks/services on different sites would produce devices
    # with the same display name, and their entity_ids would only be told
    # apart by an arbitrary HA-assigned _2 suffix. See
    # MULTI_SITE_IDENTITY_SPEC.md.
    return DeviceInfo(
        identifiers={service_device_identifier(entry_id, stack_name, service_name)},
        name=f"{stack_name} ({site}) / {service_name}",
        manufacturer="Docker Compose",
        via_device=stack_device_identifier(entry_id, stack_name),
    )


def stack_attributes(site: str, stack_name: str, kind: str) -> dict[str, str]:
    """The `site`/`stack`/`kind` attributes every stack-level entity
    exposes — same rationale as service_attributes() below, and
    deliberately the same shape (a plain function each entity calls and
    merges into its own dict, not a base-class property): none of the
    entities this is used from currently chain into
    `super().extra_state_attributes`, and two of them
    (StackUpdateAvailableSensor, on UpdateCheckCoordinator) don't even
    share a base class with the rest — see MULTI_SITE_IDENTITY_SPEC.md.

    `kind` (one of const.py's KIND_* constants) is a stable, per-entity-
    class discriminator for dashboard code: (device_id, kind) identifies
    a specific entity on a device without relying on icon (a user can
    customize an icon in the UI, silently breaking icon-based matching)
    or on parsing the generated name/entity_id.
    """
    return {"site": site, "stack": stack_name, "kind": kind}


def service_attributes(stack_name: str, service_name: str, kind: str) -> dict[str, str]:
    """The `stack`/`service`/`kind` attributes every per-service entity
    exposes — plain, un-doubled values, so dashboard code never needs to
    reverse-engineer either by parsing entity_id. See
    SERVICE_ATTRIBUTES_SPEC.md: entity_id-based parsing is fragile
    (several real stack names contain underscores) and at least one real
    string-prefix collision exists (music_assistant vs.
    music_assistant_backup) — a plain literal attribute removes the need
    for any client-side parsing/guessing at all. One canonical place for
    the key names, used consistently by every per-service entity across
    sensor.py/switch.py/button.py/binary_sensor.py, rather than the key
    names being repeated (and risking drift) at every call site.

    `kind` — see stack_attributes()'s docstring for the same rationale.
    """
    return {"stack": stack_name, "service": service_name, "kind": kind}


class StackDeviceEntity(CoordinatorEntity[StacksCoordinator]):
    """Base for a stack-level entity, attached to the stack's own device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StacksCoordinator, entry_id: str, stack_name: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._stack_name = stack_name
        # Overwritten right after by every service-level subclass
        # (service_device_info() instead) — harmless, since only the
        # final assignment in __init__ matters; see those subclasses.
        self._attr_device_info = stack_device_info(entry_id, stack_name, coordinator.site)

    @property
    def _status(self) -> StackStatus | None:
        return (self.coordinator.data or {}).get(self._stack_name)
