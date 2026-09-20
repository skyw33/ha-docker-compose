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
yet to accept setup. A reconfigure step exists for site_name only (see
async_step_reconfigure and MULTI_SITE_IDENTITY_SPEC.md) — every other
field here still has no reconfigure path; changing stacks_root/
docker_host/sidecar_container/poll_interval after setup still means
deleting and re-adding the entry.
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
    CONF_SITE_NAME,
    CONF_STACKS_ROOT,
    DEFAULT_DOCKER_HOST,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SIDECAR_CONTAINER,
    DOMAIN,
    HIGH_POLL_INTERVAL_WARNING_THRESHOLD_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)
from .discovery import discover_stacks
from .site_identity import other_site_slugs, resolve_unique_site_slug, slugify_site_name

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
        # Left blank, a default is derived from stacks_root's leaf folder
        # name and auto-suffixed if it collides with another entry's — see
        # _resolve_site_slug. An explicitly typed value that collides is
        # rejected instead (MULTI_SITE_IDENTITY_SPEC.md).
        vol.Optional(CONF_SITE_NAME, default=""): str,
    }
)


def _configured_site_entries(hass) -> list[tuple[str, str | None]]:
    return [
        (entry.entry_id, entry.options.get(CONF_SITE_NAME))
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]


def _resolve_site_slug(
    hass, site_name_input: str, default_source: str, *, exclude_entry_id: str | None
) -> tuple[str | None, str | None]:
    """Returns (resolved_slug, error_code). On error, resolved_slug is None
    and error_code is meant for the CONF_SITE_NAME field.

    default_source is the stacks_root leaf folder name, used only when
    site_name_input is blank."""
    taken = other_site_slugs(_configured_site_entries(hass), exclude_entry_id)
    if site_name_input:
        desired_slug = slugify_site_name(site_name_input)
        if desired_slug in taken:
            return None, "site_name_already_used"
        return desired_slug, None
    return resolve_unique_site_slug(slugify_site_name(default_source), taken), None


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
                        site_slug, site_error = _resolve_site_slug(
                            self.hass,
                            user_input[CONF_SITE_NAME].strip(),
                            stacks_root.name,
                            exclude_entry_id=None,
                        )
                        if site_error is not None:
                            errors[CONF_SITE_NAME] = site_error
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
                                options={CONF_SITE_NAME: site_slug},
                            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Change only the site name after initial setup — see
        MULTI_SITE_IDENTITY_SPEC.md. Deliberately not a full re-run of
        async_step_user: stacks_root/docker_host/etc. still have no
        reconfigure path (unchanged from before this spec); this adds one
        for site_name alone, since that's the only field this spec needs
        to be user-changeable after setup.

        Requires HA core >= 2024.4.0 (see hacs.json) — that's when
        SOURCE_RECONFIGURE/the "Reconfigure" UI action were introduced;
        verified directly against home-assistant/core's tagged source,
        not assumed. async_update_reload_and_abort() itself exists a bit
        earlier (2024.2.0) but is pointless without a way to reach this
        step.

        Subtlety verified against HA core's data_entry_flow.py: this
        step's *first* call is not invoked with user_input=None the way
        async_step_user's is — ConfigEntry._async_init_reconfigure() seeds
        it with the entry's current entry.data dict instead (`flow.init_step
        = "reconfigure"`, then `_async_handle_step(flow, flow.init_step,
        data)` where `data` is `entry.data | {}`). Since site_name lives in
        entry.options, never entry.data, that seed dict can never contain
        CONF_SITE_NAME — which is what safely distinguishes "flow just
        opened" from "form was actually submitted" below, without needing
        HA's own _get_reconfigure_entry() helper (that lands in HA
        2024.11, later than this integration's 2024.4 floor).
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reconfigure_entry_not_found")

        errors: dict[str, str] = {}

        if user_input is not None and CONF_SITE_NAME in user_input:
            desired = user_input[CONF_SITE_NAME].strip()
            if not desired:
                errors[CONF_SITE_NAME] = "site_name_required"
            else:
                site_slug, site_error = _resolve_site_slug(
                    self.hass, desired, desired, exclude_entry_id=entry.entry_id
                )
                if site_error is not None:
                    errors[CONF_SITE_NAME] = site_error
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        options={**entry.options, CONF_SITE_NAME: site_slug},
                        reason="reconfigure_successful",
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SITE_NAME, default=entry.options.get(CONF_SITE_NAME, "")
                    ): str
                }
            ),
            errors=errors,
        )
