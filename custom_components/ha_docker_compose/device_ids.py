"""Device-identifier construction, kept dependency-light (no homeassistant
imports) so the "what devices should exist" computation — the crux of
stale-device pruning on reload, see __init__.py — can be unit tested
directly, without a running Home Assistant instance.
"""
from __future__ import annotations

from .const import DOMAIN
from .discovery import StackInfo

DeviceIdentifier = tuple[str, str]


def stack_device_identifier(entry_id: str, stack_name: str) -> DeviceIdentifier:
    return (DOMAIN, f"{entry_id}_{stack_name}")


def service_device_identifier(entry_id: str, stack_name: str, service_name: str) -> DeviceIdentifier:
    return (DOMAIN, f"{entry_id}_{stack_name}_{service_name}")


def expected_device_identifiers(entry_id: str, stacks: list[StackInfo]) -> set[DeviceIdentifier]:
    """Every device identifier that should exist given the current,
    freshly discovered stack list — used to decide what's stale on reload.

    A stack that's simply stopped (folder still exists, no running
    containers) is still in `stacks`, so its identifiers are still
    "expected" here; this only omits stacks/services genuinely absent from
    disk as of this discovery pass.
    """
    identifiers: set[DeviceIdentifier] = set()
    for stack in stacks:
        identifiers.add(stack_device_identifier(entry_id, stack.name))
        for service in stack.service_names:
            identifiers.add(service_device_identifier(entry_id, stack.name, service))
    return identifiers
