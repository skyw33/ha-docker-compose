"""Protection labels for self-referential infrastructure services.

A service can be marked `ha_docker_compose.protection: full` via a compose
`labels:` block to suppress its action entities (switch, restart button).
Read-only observability entities (state, stats, update_available,
detected_version, latest_github_release, log_command) are never affected —
protection only restricts actions, never visibility. See
PROTECTED_STACK_SPEC.md.

If any service in a stack is protection=full, the whole stack's
stack-level action entities (switch.{stack}_running,
button.{stack}_pull_update) are suppressed too, so per-service protection
can't be bypassed by acting on the stack as a whole.

Unset (the default, for every stack that doesn't opt in — including
homeassistant's own stack, deliberately not protected per
PROTECTED_STACK_SPEC.md's "Decision" section) leaves behavior completely
unchanged — fully backward compatible, opt-in only.
"""
from __future__ import annotations

from typing import Any

PROTECTION_LABEL = "ha_docker_compose.protection"
PROTECTION_FULL = "full"


def parse_compose_labels(service_def: dict[str, Any]) -> dict[str, str]:
    """Normalize a service's `labels:` value from resolved compose config
    into a plain dict.

    Compose accepts both the list form (`- "key=value"`) and the map form
    (`key: value`) in source YAML, and `docker compose config` output can
    present either depending on path: sidecar-exec'd resolution typically
    normalizes to a map, while discovery's raw-YAML fallback (used when
    the sidecar can't be reached) preserves whatever form the source file
    used — so both must be handled here.
    """
    labels = service_def.get("labels")
    if not labels:
        return {}

    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}

    if isinstance(labels, list):
        result: dict[str, str] = {}
        for item in labels:
            if isinstance(item, str) and "=" in item:
                key, _, value = item.partition("=")
                result[key] = value
        return result

    return {}


def is_service_protected(service_def: dict[str, Any]) -> bool:
    """True if this service's resolved compose config carries
    `ha_docker_compose.protection: full`."""
    return parse_compose_labels(service_def).get(PROTECTION_LABEL) == PROTECTION_FULL


def is_stack_protected(compose_config: dict[str, Any]) -> bool:
    """True if ANY service in this stack's resolved compose config is
    protection=full — see module docstring for why that suppresses
    stack-level action entities too."""
    services = compose_config.get("services") or {}
    return any(is_service_protected(service_def) for service_def in services.values())
