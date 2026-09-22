"""Slow-interval, fully decoupled service-metadata coordinator.

Two independent, best-effort signals live here, sharing one poll cycle
(and, per service, one `get_image_labels()` call) since both are read off
the same image/container labels:

- The "latest GitHub release" lookup (unchanged from before — see
  PROJECT_SPEC.md's Update Tracking section: never linked to
  update-available or to what's currently running).
- Local version detection (LOCAL_VERSION_DETECTION_SPEC.md): an
  `org.opencontainers.image.version` label, or the image tag itself if it
  looks like a real version — zero network calls, "detect and display
  what we already know," not a comparison. Independent of both
  update-available and the GitHub lookup; none of these three signals
  imply or corroborate each other. Extended with two further fallbacks,
  checked in this order, only if every check ahead of them comes back
  empty: `verified_current_version` (UNVERIFIED_DETECTED_VERSION_SPEC.md's
  verified-current-version amendment) — a live registry-tag/digest
  cross-reference already computed by TagWalkCoordinator, read from it
  here at zero extra I/O, for a service pinned to a floating tag with
  nothing else to go on; then, strictly last resort, `assumed_version`
  (UNVERIFIED_DETECTED_VERSION_SPEC.md's original amendment) — a
  persisted snapshot of `latest_registry_tag` from the last successful
  pull, read from DigestHistoryStore here. Both are passed into
  detect_version(), which decides the actual priority order.

Polls infrequently: unauthenticated GitHub API is capped at 60
requests/hour, and neither a project's latest release nor a locally
detected version changes on the order of seconds. Runs against every
discovered stack's service regardless of the stack's current run state — a
stopped stack is still checked, it just has no container to fall back to
for either signal's label lookup.

The `org.opencontainers.image.source`/`.version` labels are checked on the
image first (the "just works, no configuration" case — authoritative when
present), falling back to the *container's* labels only if the image has
none. This lets a user manually supply either label via a `labels:` block
on the service in their compose file for images that don't set it
themselves — Compose applies `labels:` to the container it creates, not to
the image, so that override would never be visible via image inspection
alone (see oci_labels.py). The container's labels are already fetched as
part of the regular fast poll (engine.ContainerInfo.labels), so this
fallback costs no extra Engine API call; it's simply unavailable for a
stack with no currently running container for that service.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .coordinator import StacksCoordinator
from .engine import ContainerInfo, DockerEngineClient
from .github_release import OCI_SOURCE_LABEL, GitHubRelease, fetch_latest_release, parse_github_repo
from .oci_labels import resolve_label
from .storage import DigestHistoryStore
from .tag_walk_coordinator import TagWalkCoordinator
from .version_detect import detect_version

_LOGGER = logging.getLogger(__name__)

DEFAULT_GITHUB_CHECK_INTERVAL = timedelta(hours=12)


@dataclass
class ServiceMetadata:
    """The two independent best-effort signals for one service, from one
    shared label-fetch — see module docstring for why they're bundled."""

    release: GitHubRelease | None = None
    detected_version: str | None = None


class GitHubReleaseCoordinator(DataUpdateCoordinator[dict[str, dict[str, ServiceMetadata]]]):
    """Keyed `{stack_name: {service_name: ServiceMetadata}}`; a service is
    simply absent if neither signal has anything to show (no OCI labels
    either way, image not hosted on GitHub, no releases, tag doesn't look
    like a version) — all normal, expected outcomes, not errors."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: DockerEngineClient,
        stacks_coordinator: StacksCoordinator,
        digest_history: DigestHistoryStore,
        tag_walk_coordinator: TagWalkCoordinator,
        label: str,
        update_interval: timedelta = DEFAULT_GITHUB_CHECK_INTERVAL,
    ) -> None:
        # label distinguishes this coordinator's log lines from another
        # config entry's (e.g. a second Docker host) — see the identical
        # note on StacksCoordinator.
        super().__init__(
            hass,
            _LOGGER,
            name=f"ha_docker_compose_github_releases ({label})",
            update_interval=update_interval,
        )
        self._engine = engine
        self._stacks_coordinator = stacks_coordinator
        self._digest_history = digest_history
        # Cross-coordinator read, same established pattern as
        # tag_walk_coordinator.py's own read of update_coordinator's data
        # (see that module's docstring) — supplies
        # detect_version()'s verified_current_version fallback with the
        # digest cross-reference TagWalkCoordinator already computed, at
        # zero extra I/O here. See UNVERIFIED_DETECTED_VERSION_SPEC.md's
        # verified-current-version amendment.
        self._tag_walk_coordinator = tag_walk_coordinator
        self._session = async_get_clientsession(hass)

    @property
    def site(self) -> str:
        """See update_coordinator.py's identical property."""
        return self._stacks_coordinator.site

    async def _async_update_data(self) -> dict[str, dict[str, ServiceMetadata]]:
        result: dict[str, dict[str, ServiceMetadata]] = {}
        stack_statuses = self._stacks_coordinator.data or {}

        for stack_name, status in stack_statuses.items():
            services: dict[str, ServiceMetadata] = {}
            for service_name, service_def in (status.info.compose_config.get("services") or {}).items():
                image_ref = service_def.get("image")
                if not image_ref:
                    continue
                container = status.container_for_service(service_name)
                metadata = await self._check_one(stack_name, service_name, image_ref, container)
                if metadata.release is not None or metadata.detected_version is not None:
                    services[service_name] = metadata
            if services:
                result[stack_name] = services

        return result

    async def _get_image_labels(self, stack_name: str, service_name: str, image_ref: str) -> dict[str, str]:
        try:
            return await self._engine.get_image_labels(image_ref)
        except Exception as err:  # noqa: BLE001 - one bad image must not abort the whole check
            _LOGGER.debug(
                "Image label lookup failed for %s (stack %s, service %s): %s",
                image_ref,
                stack_name,
                service_name,
                err,
            )
            return {}

    async def _check_one(
        self,
        stack_name: str,
        service_name: str,
        image_ref: str,
        container: ContainerInfo | None,
    ) -> ServiceMetadata:
        image_labels = await self._get_image_labels(stack_name, service_name, image_ref)
        container_labels = container.labels if container is not None else None

        release = await self._check_github_release(
            stack_name, service_name, image_ref, image_labels, container_labels
        )
        # verified_current_version: TagWalkCoordinator's own live digest
        # cross-reference for this exact service, already computed on its
        # own (much slower) cadence — read here, not fetched, so this
        # coordinator's own poll cycle costs nothing extra for it. May be
        # None (no tag-walk data yet for this stack/service, or no match
        # found) — detect_version() treats that exactly like "not
        # supplied" and falls through.
        tag_walk_data = (self._tag_walk_coordinator.data or {}).get(stack_name, {})
        tag_walk_status = tag_walk_data.get(service_name)
        verified_current_version = (
            tag_walk_status.verified_current_version if tag_walk_status else None
        )

        # Strictly last-resort, unverified fallback — see
        # UNVERIFIED_DETECTED_VERSION_SPEC.md. Only ever reaches
        # detect_version()'s return value if every check ahead of it
        # (image label, container label, pinned-tag shape,
        # verified_current_version) comes back empty; this coordinator
        # has no opinion on that priority order, it just supplies the two
        # extra data points detect_version() itself doesn't have I/O
        # access to fetch.
        history_record = self._digest_history.get(stack_name, service_name) or {}
        assumed_version = history_record.get("assumed_version")
        detected_version = detect_version(
            image_ref,
            image_labels,
            container_labels,
            stack_name=stack_name,
            service_name=service_name,
            verified_current_version=verified_current_version,
            assumed_version=assumed_version,
        )

        return ServiceMetadata(release=release, detected_version=detected_version)

    async def _check_github_release(
        self,
        stack_name: str,
        service_name: str,
        image_ref: str,
        image_labels: dict[str, str],
        container_labels: dict[str, str] | None,
    ) -> GitHubRelease | None:
        source_url, origin = resolve_label(image_labels, container_labels, OCI_SOURCE_LABEL)
        if source_url:
            _LOGGER.debug(
                "%s (stack %s, service %s): using '%s' from the %s's labels",
                image_ref,
                stack_name,
                service_name,
                OCI_SOURCE_LABEL,
                origin,
            )
        else:
            _LOGGER.debug(
                "%s (stack %s, service %s): no '%s' label on the image%s — nothing to show for "
                "the latest-release sensor (this is expected/normal, not an error)",
                image_ref,
                stack_name,
                service_name,
                OCI_SOURCE_LABEL,
                " or its container" if container_labels is not None else " (no running container to check either)",
            )
            return None

        parsed = parse_github_repo(source_url)
        if parsed is None:
            _LOGGER.debug(
                "%s: '%s' label value %r doesn't look like a github.com repo URL — skipping",
                image_ref,
                OCI_SOURCE_LABEL,
                source_url,
            )
            return None

        owner, repo = parsed
        release = await fetch_latest_release(self._session, owner, repo)
        if release is None:
            _LOGGER.debug(
                "%s: GitHub releases/latest lookup for %s/%s returned nothing (see "
                "github_release.py logs for the reason if debug logging is enabled there too)",
                image_ref,
                owner,
                repo,
            )
        return release
