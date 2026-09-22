"""Slow-interval coordinator: native registry digest comparison.

Deliberately separate from the fast live-stats coordinator
(StacksCoordinator) — registry calls are rate limited and this data doesn't
need to be fresh to the second. Only services in a fully `running` stack are
checked; stopped/partial/orphaned stacks are skipped entirely, per spec, to
avoid wasted registry calls on inactive folders.

Remote digests come from registry_client.py (native aiohttp calls to the
Docker Registry HTTP API v2), not a skopeo subprocess — see
UPDATE_CHECK_NATIVE_SPEC.md. Per that spec's error-handling rules:
- A transient failure (RegistryError) keeps the previous poll's remote
  digest rather than resetting it, so `update_available` doesn't flap to
  "no update" on a blip.
- An unsupported registry or auth failure (401/403) is a standing
  condition, not a blip: it's surfaced as permanently unavailable for that
  service and logged once, not re-logged every poll.

Each stack's result also carries one aggregated `update_available` (OR
across its services) alongside the per-service detail — see
StackUpdateSummary — for a cheap stack-level dashboard summary
(binary_sensor.{stack}_update_available in binary_sensor.py) that needs no
extra polling or I/O beyond what this coordinator already collects.

Registry tag-list walking (latest_registry_tag, pull_target_version) used
to live in this same coordinator/cycle, but was split out to
tag_walk_coordinator.py on its own, much slower interval — see
REGISTRY_TAG_WALK_SPEC.md's "Prerequisite" amendment. A full tag-list walk
is comparatively expensive (confirmed real cost: ESPHome needs 29
paginated registry requests for 2,890 tags) and a project's available
tags change far less often than "is there a new digest for my currently
pinned tag" — running both on this coordinator's hourly cadence wasted
most of that traffic on data that rarely changes. This coordinator stays
on its original fast/hourly cadence, since freshness genuinely matters for
"should I pull" (update_available); TagWalkCoordinator reads this
coordinator's remote_digest for its own pull_target_version correlation,
rather than duplicating the digest lookup.

Also tracks each service's update_available state across cycles
(`_known_on_services`, in-memory only — same category of state as
coordinator.py's `pulling_stacks`, doesn't need to survive a restart) and
fires a scoped TagWalkCoordinator refresh the moment a service
transitions off→on — see PULL_TARGET_VERSION_SPEC.md's
update_available-transition amendment. That's the one moment
latest_registry_tag/pull_target_version actually become newly relevant,
and TagWalkCoordinator's own scheduled sweep (every 48h as of that
amendment) is too slow to catch it promptly on its own. Decoupled from a
direct import of TagWalkCoordinator via an optional callback
(`set_transition_callback()`), not a constructor dependency — the two
coordinators would otherwise need a genuine two-way reference
(TagWalkCoordinator already depends on this coordinator's remote_digest),
which isn't possible to wire at construction time in either direction
without one of them existing first; see __init__.py for how it's linked
after both are constructed.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import STACK_STATE_RUNNING
from .coordinator import StacksCoordinator
from .engine import DockerEngineClient
from .registry_client import RegistryAuthError, RegistryClient, RegistryError, RegistryUnsupportedError
from .storage import DigestHistoryStore

_LOGGER = logging.getLogger(__name__)

TransitionCallback = Callable[[set[str]], Awaitable[None]]

DEFAULT_UPDATE_CHECK_INTERVAL = timedelta(hours=1)


@dataclass
class ServiceUpdateStatus:
    image: str
    local_digest: str | None
    remote_digest: str | None
    # Standing (not transient) reasons the remote digest can't be checked at
    # all for this service — distinct from a one-off network blip.
    registry_supported: bool = True
    auth_denied: bool = False
    # From the persisted pull history (storage.py), not this check itself.
    tag: str | None = None
    last_pulled: str | None = None
    previous_digest: str | None = None

    @property
    def update_available(self) -> bool | None:
        """None means "can't tell" (no digests yet, unsupported registry, or
        auth denied) — distinct from an actual on/off comparison result."""
        if not self.registry_supported or self.auth_denied:
            return None
        if self.local_digest and self.remote_digest:
            return self.local_digest != self.remote_digest
        return None


@dataclass
class StackUpdateSummary:
    """Per-stack bundle: every service's digest-check result, plus one
    aggregated `update_available` derived from them — the same shape as
    coordinator.py's StackStatus bundling per-container detail alongside
    its own derived `state`. Pure OR aggregation over data already
    collected this cycle: no new I/O, no new polling.
    """

    services: dict[str, ServiceUpdateStatus]
    update_available: bool | None


def _aggregate_update_available(services: dict[str, ServiceUpdateStatus]) -> bool | None:
    """True if ANY service has a confirmed pending update. False only if
    EVERY service is confirmed not-pending. None otherwise — including the
    empty-dict case (nothing checked yet) and the case where nothing is
    pending but at least one service's own status is still unresolved
    (unsupported registry, auth denied, no digests yet): "no updates
    detected" would overstate what's actually known in that situation.
    """
    if not services:
        return None

    results = [status.update_available for status in services.values()]
    if any(result is True for result in results):
        return True
    if any(result is None for result in results):
        return None
    return False


class UpdateCheckCoordinator(DataUpdateCoordinator[dict[str, StackUpdateSummary]]):
    """Per-service digest comparison, keyed `{stack_name: StackUpdateSummary}`."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: DockerEngineClient,
        stacks_coordinator: StacksCoordinator,
        digest_history: DigestHistoryStore,
        registry_client: RegistryClient,
        label: str,
        update_interval: timedelta = DEFAULT_UPDATE_CHECK_INTERVAL,
    ) -> None:
        # label distinguishes this coordinator's log lines from another
        # config entry's (e.g. a second Docker host) — see the identical
        # note on StacksCoordinator.
        super().__init__(
            hass,
            _LOGGER,
            name=f"ha_docker_compose_update_checks ({label})",
            update_interval=update_interval,
        )
        self._engine = engine
        self._stacks_coordinator = stacks_coordinator
        self._digest_history = digest_history
        # Shared, not constructed here — one RegistryClient per config
        # entry, passed to both this coordinator and TagWalkCoordinator
        # (see __init__.py), so its per-registry concurrency limit
        # (MAX_CONCURRENT_REQUESTS_PER_REGISTRY) actually bounds the two
        # coordinators' independently-scheduled sweeps together, not just
        # each in isolation — two unrelated schedules bursting past each
        # other with no shared limit is exactly the confirmed failure
        # mode (simultaneous 429s across several stacks during a
        # Reload-triggered sweep). See registry_client.py.
        self._registry_client = registry_client
        # (stack_name, service_name) pairs already logged for a standing
        # (non-transient) condition, so we warn once instead of every poll.
        self._warned_unsupported: set[tuple[str, str]] = set()
        self._warned_auth_denied: set[tuple[str, str]] = set()
        # Services this coordinator currently believes have an update
        # pending, as of the last cycle that computed a value for them —
        # in-memory only, never persisted, a restart naturally clears it
        # (same category of state as coordinator.py's pulling_stacks).
        # Membership (not a three-state dict) is enough to distinguish
        # every case that matters: a key absent here means "not known
        # on" — covers both "confirmed off/None last cycle" AND "never
        # computed before" identically, which is exactly what makes a
        # service's very first computation coming back True correctly
        # count as a transition (see _async_update_data()) without any
        # separate "have we seen this service before" bookkeeping.
        self._known_on_services: set[tuple[str, str]] = set()
        self._transition_callback: TransitionCallback | None = None

    @property
    def site(self) -> str:
        """This entry's resolved site slug — reads through to the
        StacksCoordinator this was constructed with, rather than copying
        it, so it can't go stale. See coordinator.py's own `site` and
        MULTI_SITE_IDENTITY_SPEC.md; used by service_device_info() calls
        for entities bound to this coordinator instead."""
        return self._stacks_coordinator.site

    def set_transition_callback(self, callback: TransitionCallback) -> None:
        """Called once from __init__.py after TagWalkCoordinator exists —
        see module docstring for why this is a late-bound callback rather
        than a constructor dependency."""
        self._transition_callback = callback

    async def _async_update_data(self) -> dict[str, StackUpdateSummary]:
        result: dict[str, StackUpdateSummary] = {}
        stack_statuses = self._stacks_coordinator.data or {}
        previous_data = self.data or {}
        # Stacks with at least one service that transitioned off→on (or
        # had its first-ever computation come back on) this cycle — see
        # PULL_TARGET_VERSION_SPEC.md's update_available-transition
        # amendment. A set, not a list: multiple transitioning services
        # in the same stack only need that stack refreshed once.
        transitioned_stacks: set[str] = set()

        for stack_name, status in stack_statuses.items():
            if status.state != STACK_STATE_RUNNING:
                continue

            previous_services = previous_data.get(stack_name)

            services: dict[str, ServiceUpdateStatus] = {}
            for service_name, service_def in (status.info.compose_config.get("services") or {}).items():
                image_ref = service_def.get("image")
                if not image_ref:
                    continue
                previous = previous_services.services.get(service_name) if previous_services else None
                service_status = await self._check_one(
                    stack_name, service_name, image_ref, previous
                )
                services[service_name] = service_status

                service_key = (stack_name, service_name)
                is_on_now = service_status.update_available is True
                if is_on_now:
                    if service_key not in self._known_on_services:
                        transitioned_stacks.add(stack_name)
                    self._known_on_services.add(service_key)
                else:
                    self._known_on_services.discard(service_key)

            if services:
                result[stack_name] = StackUpdateSummary(
                    services=services, update_available=_aggregate_update_available(services)
                )

        if transitioned_stacks:
            self._fire_transition_callback(transitioned_stacks)

        return result

    def _fire_transition_callback(self, transitioned_stacks: set[str]) -> None:
        if self._transition_callback is None:
            _LOGGER.debug(
                "update_available transitioned off→on for %s, but no transition callback is "
                "wired up yet — nothing to trigger",
                sorted(transitioned_stacks),
            )
            return

        callback = self._transition_callback

        async def _run() -> None:
            try:
                await callback(transitioned_stacks)
            except Exception:  # noqa: BLE001 - a background trigger failing must not crash anything else
                _LOGGER.exception(
                    "Stack-scoped tag-walk refresh trigger failed for %s",
                    sorted(transitioned_stacks),
                )

        # Backgrounded, not awaited inline — this coordinator's own cycle
        # (and update_available's freshness for every other consumer)
        # shouldn't wait on a scoped registry walk that isn't this
        # cycle's job to produce. Same hass.async_create_task() pattern
        # already used for backgrounded work in pull_jobs.py.
        self.hass.async_create_task(
            _run(), name=f"ha_docker_compose tag-walk transition trigger: {sorted(transitioned_stacks)}"
        )

    async def _check_one(
        self,
        stack_name: str,
        service_name: str,
        image_ref: str,
        previous: ServiceUpdateStatus | None,
    ) -> ServiceUpdateStatus:
        try:
            local_digest = await self._engine.get_image_repo_digest(image_ref)
        except Exception:  # noqa: BLE001 - one bad image must not abort the whole check
            _LOGGER.exception("Local digest lookup failed for %s/%s", stack_name, service_name)
            local_digest = None

        service_key = (stack_name, service_name)
        registry_supported = True
        auth_denied = False
        # Default to the last known remote digest; a transient failure below
        # leaves it as-is instead of resetting to None.
        remote_digest = previous.remote_digest if previous else None

        try:
            remote_digest = await self._registry_client.get_manifest_digest(image_ref)
            self._warned_unsupported.discard(service_key)
            self._warned_auth_denied.discard(service_key)
        except RegistryUnsupportedError:
            registry_supported = False
            if service_key not in self._warned_unsupported:
                _LOGGER.warning(
                    "Update checking not supported for the registry of image %s (stack %s, service %s)",
                    image_ref,
                    stack_name,
                    service_name,
                )
                self._warned_unsupported.add(service_key)
        except RegistryAuthError:
            auth_denied = True
            if service_key not in self._warned_auth_denied:
                _LOGGER.warning(
                    "Registry denied access to image %s (stack %s, service %s) — "
                    "private image without configured credentials?",
                    image_ref,
                    stack_name,
                    service_name,
                )
                self._warned_auth_denied.add(service_key)
        except RegistryError as err:
            _LOGGER.debug(
                "Registry digest check failed for %s (stack %s, service %s), keeping last known value: %s",
                image_ref,
                stack_name,
                service_name,
                err,
            )

        history_record = self._digest_history.get(stack_name, service_name) or {}
        _LOGGER.debug(
            "_check_one(%s, %s): local_digest=%s, history_record=%r (last_pulled will be %r)",
            stack_name,
            service_name,
            local_digest,
            history_record,
            history_record.get("pulled_at"),
        )

        return ServiceUpdateStatus(
            image=image_ref,
            local_digest=local_digest,
            remote_digest=remote_digest,
            registry_supported=registry_supported,
            auth_denied=auth_denied,
            tag=history_record.get("tag"),
            last_pulled=history_record.get("pulled_at"),
            previous_digest=history_record.get("previous_digest"),
        )
