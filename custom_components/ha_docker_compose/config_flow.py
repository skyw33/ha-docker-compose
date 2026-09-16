"""Config flow for HA Docker Compose Manager.

Single-step flow: stacks_root path, Docker daemon address (unix:// or
tcp://, see SOCKET_PROXY_SPEC.md), the sidecar container name, and the
fast stats/state poll interval (per entry/node, not per stack — see
POLL_INTERVAL_SPEC.md). Validates by actually running discovery against
the path, so a misconfigured root is caught at setup time rather than
surfacing as a broken integration later. The Docker host address is only
structurally validated (scheme check) — actual reachability (proxy up,
sidecar running) is a runtime concern surfaced through the usual entity
error paths, not a config-flow gate, since none of that needs to be live
yet to accept setup. No reconfigure step exists yet for any field in this
flow (this integration doesn't have one at all currently) — changing the
poll interval after setup means deleting and re-adding the entry, same as
every other field here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DOCKER_HOST,
    CONF_POLL_INTERVAL,
    CONF_SIDECAR_CONTAINER,
    CONF_STACKS_ROOT,
    DEFAULT_DOCKER_HOST,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SIDECAR_CONTAINER,
    DOMAIN,
    HIGH_POLL_INTERVAL_WARNING_THRESHOLD_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)
from .discovery import discover_stacks

_LOGGER = logging.getLogger(__name__)

_VALID_DOCKER_HOST_SCHEMES = ("unix://", "tcp://")

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STACKS_ROOT): str,
        vol.Optional(CONF_DOCKER_HOST, default=DEFAULT_DOCKER_HOST): str,
        vol.Optional(CONF_SIDECAR_CONTAINER, default=DEFAULT_SIDECAR_CONTAINER): str,
        # No hard maximum — a deliberately long interval for a remote node
        # is a valid choice. High values just get a one-time log warning
        # below, not a validation error.
        vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL_SECONDS): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL_SECONDS)
        ),
    }
)


class HaDockerComposeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Docker Compose Manager."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            stacks_root = Path(user_input[CONF_STACKS_ROOT]).expanduser()
            docker_host = user_input[CONF_DOCKER_HOST].strip()
            poll_interval = user_input[CONF_POLL_INTERVAL]  # already >= MIN_POLL_INTERVAL_SECONDS,
            # enforced by the schema's vol.Range before this method is even called.

            if poll_interval > HIGH_POLL_INTERVAL_WARNING_THRESHOLD_SECONDS:
                _LOGGER.warning(
                    "Poll interval of %ds for stacks root %s is well above the %ds "
                    "out-of-the-box default — stats/state sensors will feel noticeably less "
                    "'live' at this cadence. Proceeding anyway since this may be a deliberate "
                    "choice (e.g. a remote node over a slow link).",
                    poll_interval,
                    stacks_root,
                    DEFAULT_POLL_INTERVAL_SECONDS,
                )

            if not stacks_root.is_dir():
                errors[CONF_STACKS_ROOT] = "stacks_root_not_found"
            elif not docker_host.startswith(_VALID_DOCKER_HOST_SCHEMES):
                errors[CONF_DOCKER_HOST] = "invalid_docker_host"
            else:
                try:
                    stacks = await discover_stacks(stacks_root)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Stack discovery failed during config flow")
                    errors["base"] = "discovery_failed"
                else:
                    if not stacks:
                        errors[CONF_STACKS_ROOT] = "no_stacks_found"
                    else:
                        await self.async_set_unique_id(str(stacks_root))
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=stacks_root.name,
                            data={
                                CONF_STACKS_ROOT: str(stacks_root),
                                CONF_DOCKER_HOST: docker_host,
                                CONF_SIDECAR_CONTAINER: user_input[CONF_SIDECAR_CONTAINER],
                                CONF_POLL_INTERVAL: poll_interval,
                            },
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
