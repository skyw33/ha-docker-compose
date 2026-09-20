"""HA Docker Compose Manager.

Compose-first stack management: compose files on disk are the source of
truth, containers are just the runtime result of them. See PROJECT_SPEC.md
at the repo root for the full design.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .compose import ComposeExecutor
from .const import (
    CONF_DOCKER_HOST,
    CONF_POLL_INTERVAL,
    CONF_SIDECAR_CONTAINER,
    CONF_SITE_NAME,
    CONF_STACKS_ROOT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SIDECAR_CONTAINER,
    DOMAIN,
)
from .coordinator import StacksCoordinator
from .device_ids import expected_device_identifiers
from .discovery import StackInfo, discover_stacks
from .engine import DockerEngineClient
from .github_coordinator import GitHubReleaseCoordinator
from .pull_jobs import PullJobRunner, PullJobStore
from .site_identity import other_site_slugs, resolve_unique_site_slug, slugify_site_name
from .storage import DigestHistoryStore
from .tag_walk_coordinator import TagWalkCoordinator
from .update_coordinator import UpdateCheckCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA Docker Compose Manager from a config entry."""
    stacks_root = Path(entry.data[CONF_STACKS_ROOT])
    sidecar_container = entry.data.get(CONF_SIDECAR_CONTAINER, DEFAULT_SIDECAR_CONTAINER)
    # .get() with a fallback, not entry.data[...] — an entry created before
    # this field existed has no key for it at all; falling back to the
    # same value the hardcoded constant used to be keeps it behaving
    # identically to before, no re-setup required. See POLL_INTERVAL_SPEC.md.
    poll_interval = timedelta(
        seconds=entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)
    )
    # Used only to tell this entry's coordinators apart in logs when more
    # than one entry is running (e.g. two Docker hosts) — the stacks_root
    # path, not entry.title, since two entries could share the same leaf
    # folder name (title is just stacks_root.name) but never the same
    # full path (config-flow already enforces that as the entry's own
    # uniqueness key).
    coordinator_label = str(stacks_root)

    site = _resolve_site(hass, entry, stacks_root)

    engine = DockerEngineClient(entry.data[CONF_DOCKER_HOST])
    await engine.connect()

    # Once per entry load, refreshed on every reload since this whole
    # function (and the StacksCoordinator it constructs) runs fresh each
    # time — see MULTI_SITE_IDENTITY_SPEC.md's CPU-percentage-of-host
    # amendment. None on failure is expected and handled downstream
    # (get_host_cpu_count() already logs its own warning); not fatal to
    # setup, since the per-site CPU total sensor has its own
    # online_cpus-based fallback for exactly this case.
    cpu_cores = await engine.get_host_cpu_count()

    compose_executor = ComposeExecutor(engine, sidecar_container)

    stacks = await discover_stacks(stacks_root, compose_executor)
    _LOGGER.info(
        "Discovered %d stack(s) under %s: %s",
        len(stacks),
        stacks_root,
        ", ".join(s.name for s in stacks) or "(none)",
    )

    _async_prune_stale_devices(hass, entry, stacks)

    coordinator = StacksCoordinator(
        hass, engine, stacks, coordinator_label, poll_interval, site=site, cpu_cores=cpu_cores
    )
    await coordinator.async_config_entry_first_refresh()

    digest_history = DigestHistoryStore(hass, entry.entry_id)
    await digest_history.async_load()

    update_coordinator = UpdateCheckCoordinator(
        hass, engine, coordinator, digest_history, coordinator_label
    )
    await update_coordinator.async_config_entry_first_refresh()

    # Deliberately after update_coordinator's first refresh, not before —
    # this coordinator's pull_target_version correlation reads
    # update_coordinator's already-computed remote_digest rather than
    # re-fetching it, so the first tag-walk cycle needs that data to
    # already exist. See tag_walk_coordinator.py and
    # REGISTRY_TAG_WALK_SPEC.md's "Prerequisite" amendment.
    tag_walk_coordinator = TagWalkCoordinator(
        hass, coordinator, update_coordinator, coordinator_label
    )
    await tag_walk_coordinator.async_config_entry_first_refresh()

    # Wired here rather than passed to UpdateCheckCoordinator's
    # constructor: TagWalkCoordinator itself depends on update_coordinator
    # (reads its already-computed remote_digest), so a direct two-way
    # constructor reference between the two isn't possible — one of them
    # has to exist first. See update_coordinator.py's module docstring and
    # PULL_TARGET_VERSION_SPEC.md's update_available-transition amendment.
    update_coordinator.set_transition_callback(tag_walk_coordinator.async_refresh_stacks)

    github_coordinator = GitHubReleaseCoordinator(
        hass, engine, coordinator, digest_history, coordinator_label
    )
    await github_coordinator.async_config_entry_first_refresh()

    pull_job_store = PullJobStore(hass, entry.entry_id)
    pull_job_runner = PullJobRunner(
        hass,
        engine,
        compose_executor,
        coordinator,
        update_coordinator,
        tag_walk_coordinator,
        digest_history,
        pull_job_store,
    )
    # Picks up any pull/recreate left in-flight by a process that didn't
    # survive to finish watching it (see DETACHED_EXEC_SPEC.md) — must run
    # after `coordinator` has its stack list (for looking up compose
    # config by name) and after `update_coordinator`/`digest_history` (a
    # resumed job that turns out to already be done needs both to finish
    # processing it immediately).
    await pull_job_runner.resume_all()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "stacks": stacks,
        "engine": engine,
        "compose_executor": compose_executor,
        "coordinator": coordinator,
        "digest_history": digest_history,
        "update_coordinator": update_coordinator,
        "tag_walk_coordinator": tag_walk_coordinator,
        "github_coordinator": github_coordinator,
        "pull_job_runner": pull_job_runner,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _resolve_site(hass: HomeAssistant, entry: ConfigEntry, stacks_root: Path) -> str:
    """Returns this entry's site slug, resolving and persisting a default
    on first load if none is stored yet — see MULTI_SITE_IDENTITY_SPEC.md.

    Existing entries from before this spec have no CONF_SITE_NAME in
    entry.options at all; no migration is needed for them to keep
    working, but the *default* still has to be genuinely unique across
    entries, and site uniqueness is a cross-entry property — computing it
    fresh on every load (without persisting) would make the resolved slug
    depend on which entry happens to load first, which can vary across HA
    restarts. Persisting it the first time this runs removes that
    instability: after this, only an explicit reconfigure ever changes it.
    """
    site = entry.options.get(CONF_SITE_NAME)
    other_slugs = other_site_slugs(
        [
            (other.entry_id, other.options.get(CONF_SITE_NAME))
            for other in hass.config_entries.async_entries(DOMAIN)
        ],
        entry.entry_id,
    )

    if site is None:
        site = resolve_unique_site_slug(slugify_site_name(stacks_root.name), other_slugs)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_SITE_NAME: site}
        )
        _LOGGER.info("Resolved and persisted site name '%s' for stacks root %s", site, stacks_root)
    elif site in other_slugs:
        # Structurally shouldn't happen once every entry has gone through
        # this same resolution at least once (config_flow.py's own
        # collision check, plus the branch above, both prevent it going
        # forward) — but two *legacy* entries loading for the first time
        # in the same startup, both deriving the same default from
        # identically-named stacks_root leaf folders, could still land
        # here simultaneously depending on load order. Loud, not silent:
        # a log warning per the spec (no Repair issue for this stage).
        _LOGGER.warning(
            "Site name '%s' for stacks root %s is also used by another Docker Compose Manager "
            "entry — set a distinct site name for one of them via that entry's Reconfigure "
            "option (Settings > Devices & Services)",
            site,
            stacks_root,
        )

    return site


def _async_prune_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, stacks: list[StackInfo]
) -> None:
    """Remove devices for stacks/services no longer found on disk as of
    this reload — and, via Home Assistant's own device-removal cascade,
    every entity attached to them.

    Only ever removes what's genuinely absent from `stacks`: a stack that
    still has a folder but no running containers (state=stopped) is still
    in `stacks` and its device/entities are left completely alone. This
    runs on every async_setup_entry — i.e. every reload — so editing a
    compose file to drop a service, or deleting a stack folder outright,
    is fully reconciled (removed entities included, not just new ones
    picked up) the next time the entry is reloaded.
    """
    device_registry = dr.async_get(hass)
    expected = expected_device_identifiers(entry.entry_id, stacks)

    for device in list(dr.async_entries_for_config_entry(device_registry, entry.entry_id)):
        if device.identifiers.isdisjoint(expected):
            _LOGGER.info(
                "Removing stale device '%s' (stack or service no longer found on disk)",
                device.name,
            )
            device_registry.async_remove_device(device.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["engine"].close()
    return unload_ok
