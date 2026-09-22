"""Slow-interval coordinator: registry tag-list walk.

Produces two signals per service, both purely informational (neither ever
feeds `update_available`, which stays digest-comparison-only on
update_coordinator.py's own, much faster cadence):

- `latest_registry_tag` — the newest final version this project has
  published anywhere on the registry, independent of what's pinned.
- `pull_target_version` — which published version tag, if any, currently
  shares the exact same digest as the service's own pinned tag right now
  (i.e. "if I pull right now, what version would I actually end up
  running"). See REGISTRY_TAG_WALK_SPEC.md's "Pull-target version" spec.

Deliberately on its own, much slower interval than UpdateCheckCoordinator
(DEFAULT_TAG_WALK_INTERVAL — see below — vs. the digest check's 1h) — see
REGISTRY_TAG_WALK_SPEC.md's "Prerequisite" amendment. A full tag-list walk
is comparatively expensive (confirmed real cost: ESPHome needs 29
paginated registry requests for 2,890 tags), and a project's available
tags change far less often than "is there a new digest for my currently
pinned tag" — running both on the digest check's hourly cadence wasted
most of that traffic on data that rarely changes.

pull_target_version reuses update_coordinator.py's own last-known
remote_digest for the pinned tag (a cross-coordinator read, same pattern
already used elsewhere in this integration, e.g. this coordinator's own
sibling coordinators reading StacksCoordinator.data) rather than
re-fetching it here — no reason to pay for a second digest lookup of the
exact same tag just because the two coordinators run on different
schedules. That value may be up to an hour stale relative to this
coordinator's own run, which is an acceptable trade per this project's
"stale is fine, wrong is not" principle already applied to every other
slow-interval signal.

**Two entry points, one shared per-stack implementation** (`_walk_stack()`)
— see PULL_TARGET_VERSION_SPEC.md's update_available-transition amendment:

- `_async_update_data()` — the normal scheduled full sweep (every
  DEFAULT_TAG_WALK_INTERVAL), covering every running stack, run
  concurrently via `asyncio.gather()` (a sequential loop over 15+ stacks
  would risk reintroducing the exact serial-poll performance regression
  fixed earlier in this project — see POLL_INTERVAL_SPEC.md's history).
  With the event trigger below now handling the common,
  time-sensitive case, this scheduled sweep's role shrank to a backstop
  for the rarer "already outdated, and the specifics changed again while
  still outdated" case — hence the interval was lengthened (18h → 48h)
  alongside adding that trigger, not on its own.
- `async_refresh_stacks(stack_names)` — a scoped, out-of-cycle refresh for
  just the given stacks, triggered by `UpdateCheckCoordinator` the moment
  one of its services' `update_available` transitions off→on (the one
  moment this data actually becomes newly relevant). Merges into
  `self.data` alongside every other stack's last-known data — never a
  full-coordinator refresh for a single stack's transition.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import STACK_STATE_RUNNING
from .coordinator import StacksCoordinator, StackStatus
from .image_ref import repo_name
from .protection import parse_compose_labels
from .registry_client import RegistryAuthError, RegistryClient, RegistryError, RegistryUnsupportedError
from .tag_filter import (
    TAG_EXCLUDE_LABEL,
    TAG_INCLUDE_LABEL,
    filter_tags,
    is_not_behind,
    rank_final_versions,
    select_newest_version_tag,
)
from .update_coordinator import StackUpdateSummary, UpdateCheckCoordinator

_LOGGER = logging.getLogger(__name__)

# Originally 18h ("12-24 hours is a reasonable starting point" per
# REGISTRY_TAG_WALK_SPEC.md). Lengthened to 48h once the
# update_available-transition trigger (async_refresh_stacks(), fired by
# UpdateCheckCoordinator) took over as the primary freshness mechanism
# for the common, time-sensitive case — this scheduled sweep is now
# purely a backstop for the rarer case an edge trigger alone wouldn't
# catch: a stack that's already outdated, whose specific newest tag
# changes again while still outdated (no new off→on edge to fire on).
# Not user-configurable for v1, same as UpdateCheckCoordinator's/
# GitHubReleaseCoordinator's own hardcoded intervals.
DEFAULT_TAG_WALK_INTERVAL = timedelta(hours=48)

# Bounded search for pull_target_version — see REGISTRY_TAG_WALK_SPEC.md's
# "Cost consideration": a manifest-digest lookup per candidate, so this is
# capped to the newest N final-release candidates (rank_final_versions()
# is already newest-first), stopping at the first digest match, rather
# than digest-checking a project's entire historical tag list. In the
# overwhelmingly common case (already current, or a handful of versions
# behind) the match is found within the first few lookups.
PULL_TARGET_MAX_DIGEST_LOOKUPS = 15


@dataclass
class ServiceTagWalkStatus:
    latest_registry_tag: str | None = None
    pull_target_version: str | None = None
    # Which candidate tag, if any, shares the *locally running* image's
    # digest right now — i.e. a live, verified answer to "what version is
    # actually running", for a service pinned to a floating tag (stable,
    # latest, edge, ...) where the tag itself carries no version
    # information to parse. Reuses pull_target_version's own search
    # result at zero extra cost whenever the service isn't behind (local
    # digest == the pinned tag's remote digest, so the same match
    # answers both questions); only runs its own bounded second search
    # when the service is behind. See
    # UNVERIFIED_DETECTED_VERSION_SPEC.md's verified-current-version
    # amendment — distinct from (and preferred over) detect_version()'s
    # own assumed_version fallback, which is a stale, unverified snapshot
    # from the last pull rather than a live digest match.
    verified_current_version: str | None = None


class TagWalkCoordinator(DataUpdateCoordinator[dict[str, dict[str, ServiceTagWalkStatus]]]):
    """Keyed `{stack_name: {service_name: ServiceTagWalkStatus}}`."""

    def __init__(
        self,
        hass: HomeAssistant,
        stacks_coordinator: StacksCoordinator,
        update_coordinator: UpdateCheckCoordinator,
        label: str,
        update_interval: timedelta = DEFAULT_TAG_WALK_INTERVAL,
    ) -> None:
        # label distinguishes this coordinator's log lines from another
        # config entry's (e.g. a second Docker host) — see the identical
        # note on StacksCoordinator.
        super().__init__(
            hass,
            _LOGGER,
            name=f"ha_docker_compose_tag_walk ({label})",
            update_interval=update_interval,
        )
        self._stacks_coordinator = stacks_coordinator
        self._update_coordinator = update_coordinator
        self._registry_client = RegistryClient(async_get_clientsession(hass))
        # (stack_name, service_name) pairs already logged for a standing
        # (non-transient) condition, so we warn once instead of every poll.
        self._warned_unsupported: set[tuple[str, str]] = set()
        self._warned_auth_denied: set[tuple[str, str]] = set()

    @property
    def site(self) -> str:
        """See update_coordinator.py's identical property."""
        return self._stacks_coordinator.site

    async def _async_update_data(self) -> dict[str, dict[str, ServiceTagWalkStatus]]:
        stack_statuses = self._stacks_coordinator.data or {}
        running = {
            name: status
            for name, status in stack_statuses.items()
            if status.state == STACK_STATE_RUNNING
        }
        if not running:
            return {}

        digest_data = self._update_coordinator.data or {}
        names = list(running)
        # Concurrent, not sequential — a serial loop over 15+ stacks would
        # risk reintroducing the exact serial-poll performance regression
        # fixed earlier in this project (see module docstring).
        results = await asyncio.gather(
            *(
                self._walk_stack(name, running[name], digest_data.get(name))
                for name in names
            )
        )

        result: dict[str, dict[str, ServiceTagWalkStatus]] = {}
        for stack_name, services in zip(names, results):
            if services:
                result[stack_name] = services
        return result

    async def async_refresh_stacks(self, stack_names: set[str]) -> None:
        """Scoped, out-of-cycle tag walk for just the given stacks — see
        PULL_TARGET_VERSION_SPEC.md's update_available-transition
        amendment. Triggered by UpdateCheckCoordinator the moment one of
        its services' update_available flips off→on (the one moment this
        data actually becomes newly relevant), not a full-coordinator
        refresh: only the given stacks are re-walked, merged into
        self.data alongside every other stack's untouched last-known
        data, and listeners are notified the same way a normal scheduled
        refresh does.

        A stack no longer running (or no longer known at all — e.g.
        removed between the triggering digest check and this call) is
        silently skipped, not an error: the transition that triggered
        this may already be moot by the time it runs.
        """
        if not stack_names:
            return

        stack_statuses = self._stacks_coordinator.data or {}
        targets = {
            name: status
            for name, status in stack_statuses.items()
            if name in stack_names and status.state == STACK_STATE_RUNNING
        }
        if not targets:
            _LOGGER.debug(
                "async_refresh_stacks(%s): none of these are currently running stacks — nothing "
                "to refresh",
                sorted(stack_names),
            )
            return

        _LOGGER.info(
            "Stack-scoped tag-walk refresh triggered for %s (update_available transition)",
            sorted(targets),
        )

        digest_data = self._update_coordinator.data or {}
        names = list(targets)
        results = await asyncio.gather(
            *(
                self._walk_stack(name, targets[name], digest_data.get(name))
                for name in names
            )
        )

        merged = dict(self.data or {})
        for stack_name, services in zip(names, results):
            if services:
                merged[stack_name] = services
            else:
                merged.pop(stack_name, None)

        self.async_set_updated_data(merged)

    async def _walk_stack(
        self,
        stack_name: str,
        status: StackStatus,
        digest_summary: StackUpdateSummary | None,
    ) -> dict[str, ServiceTagWalkStatus]:
        """Every service's tag-walk result for one stack — the single
        implementation shared by both the scheduled full sweep and the
        scoped async_refresh_stacks(), so fetch/filter/rank logic exists
        in exactly one place (this project has already hit real bugs from
        the same logic existing in two places and drifting apart — see
        has_version_structure()'s consolidation history in
        LOCAL_VERSION_DETECTION_SPEC.md)."""
        previous_services = (self.data or {}).get(stack_name)

        services: dict[str, ServiceTagWalkStatus] = {}
        for service_name, service_def in (status.info.compose_config.get("services") or {}).items():
            image_ref = service_def.get("image")
            if not image_ref:
                continue

            pinned_digest = None
            local_digest = None
            if digest_summary is not None:
                digest_status = digest_summary.services.get(service_name)
                if digest_status is not None:
                    pinned_digest = digest_status.remote_digest
                    local_digest = digest_status.local_digest

            previous = previous_services.get(service_name) if previous_services else None
            services[service_name] = await self._check_one(
                stack_name, service_name, service_def, image_ref, pinned_digest, local_digest, previous
            )

        return services

    async def _check_one(
        self,
        stack_name: str,
        service_name: str,
        service_def: dict,
        image_ref: str,
        pinned_digest: str | None,
        local_digest: str | None,
        previous: ServiceTagWalkStatus | None,
    ) -> ServiceTagWalkStatus:
        service_key = (stack_name, service_name)

        try:
            raw_tags = await self._registry_client.list_tags(
                image_ref, log_context=f"stack {stack_name}, service {service_name}"
            )
        except RegistryUnsupportedError:
            if service_key not in self._warned_unsupported:
                _LOGGER.warning(
                    "Tag walk not supported for the registry of image %s (stack %s, service %s)",
                    image_ref,
                    stack_name,
                    service_name,
                )
                self._warned_unsupported.add(service_key)
            return ServiceTagWalkStatus()
        except RegistryAuthError:
            if service_key not in self._warned_auth_denied:
                _LOGGER.warning(
                    "Registry denied access to image %s (stack %s, service %s) — "
                    "private image without configured credentials?",
                    image_ref,
                    stack_name,
                    service_name,
                )
                self._warned_auth_denied.add(service_key)
            return ServiceTagWalkStatus()
        except RegistryError as err:
            _LOGGER.debug(
                "Tag list fetch failed for %s (stack %s, service %s), keeping last known "
                "tag-walk values: %s",
                image_ref,
                stack_name,
                service_name,
                err,
            )
            return previous or ServiceTagWalkStatus()

        self._warned_unsupported.discard(service_key)
        self._warned_auth_denied.discard(service_key)

        labels = parse_compose_labels(service_def)
        tag_include = labels.get(TAG_INCLUDE_LABEL) or None
        tag_exclude = labels.get(TAG_EXCLUDE_LABEL) or None
        # Full content, not just a count — deliberately verbose, same
        # rationale as this integration's other tag-walk debug logs: the
        # exact boundary between "what the registry sent us" and "what our
        # own filtering/selection did with it".
        _LOGGER.debug(
            "Tag walk for %s (stack %s, service %s): raw_tags(%d)=%r",
            image_ref,
            stack_name,
            service_name,
            len(raw_tags),
            raw_tags,
        )
        candidates = filter_tags(raw_tags, tag_include=tag_include, tag_exclude=tag_exclude)
        _LOGGER.debug(
            "Tag walk for %s (stack %s, service %s): tag_include=%r, tag_exclude=%r, "
            "candidates(%d)=%r",
            image_ref,
            stack_name,
            service_name,
            tag_include,
            tag_exclude,
            len(candidates),
            candidates,
        )

        latest_registry_tag = select_newest_version_tag(candidates)
        # Shared by both digest searches below — computed once, not once
        # per search, since it's the same bounded candidate set either way
        # (see PULL_TARGET_MAX_DIGEST_LOOKUPS and rank_final_versions()'s
        # own tie-break for why this ordering is deterministic).
        ranked = rank_final_versions(candidates)[:PULL_TARGET_MAX_DIGEST_LOOKUPS]

        pull_target_version = None
        if pinned_digest:
            pull_target_version = await self._search_ranked_for_digest(
                stack_name, service_name, image_ref, ranked, pinned_digest, "pull_target_version"
            )

        verified_current_version = None
        if local_digest:
            if is_not_behind(pinned_digest, local_digest):
                # Not behind: the search above already answered "which
                # candidate has this exact digest" using the same target
                # value (local_digest == pinned_digest here), so its
                # result is the current version too — reusing it costs
                # zero extra registry calls. Re-running the same search
                # against the same target digest would just repeat the
                # same lookups for the same answer.
                verified_current_version = pull_target_version
            else:
                # Behind (or no digest check has run yet to compare
                # against) — a genuinely different target digest needs
                # its own search, reusing the same already-fetched/ranked
                # candidate list.
                verified_current_version = await self._search_ranked_for_digest(
                    stack_name,
                    service_name,
                    image_ref,
                    ranked,
                    local_digest,
                    "verified_current_version",
                )

        _LOGGER.debug(
            "Tag walk for %s (stack %s, service %s): latest_registry_tag=%r, "
            "pull_target_version=%r (pinned_digest=%r), verified_current_version=%r "
            "(local_digest=%r)",
            image_ref,
            stack_name,
            service_name,
            latest_registry_tag,
            pull_target_version,
            pinned_digest,
            verified_current_version,
            local_digest,
        )

        return ServiceTagWalkStatus(
            latest_registry_tag=latest_registry_tag,
            pull_target_version=pull_target_version,
            verified_current_version=verified_current_version,
        )

    async def _search_ranked_for_digest(
        self,
        stack_name: str,
        service_name: str,
        image_ref: str,
        ranked: list[str],
        target_digest: str,
        purpose: str,
    ) -> str | None:
        """Which candidate tag, if any, currently shares target_digest —
        shared by both pull_target_version's search (target_digest =
        the pinned tag's remote digest — "what you'd get if you pulled
        right now") and verified_current_version's own second search
        (target_digest = local_digest — "what's actually running right
        now"), so this manifest-lookup loop and its error handling exist
        in exactly one place rather than two copies drifting apart.

        `ranked` is expected to already be rank_final_versions()'d and
        bounded (PULL_TARGET_MAX_DIGEST_LOOKUPS) by the caller — this
        method doesn't re-rank, since both callers in _check_one() need
        the identical bounded list and there's no reason to compute it
        twice. Searches in the order given (newest-first, per
        rank_final_versions()'s own tie-break, so the first match found
        is always the newest/most-specific one if more than one
        candidate happens to share the digest). Returns None (never
        guesses) if nothing matches within the given list — expected for
        some projects (e.g. pinned to a tag whose current build has no
        corresponding numbered version tag at all). `purpose` is only
        for the per-candidate-failure debug log below, to tell the two
        callers' log lines apart.
        """
        repo = repo_name(image_ref)

        for tag in ranked:
            candidate_ref = f"{repo}:{tag}"
            try:
                digest = await self._registry_client.get_manifest_digest(candidate_ref)
            except RegistryError as err:
                # One candidate failing (transient blip, or an oddball tag
                # the registry rejects) must not abort the whole search —
                # just move on to the next candidate.
                _LOGGER.debug(
                    "%s search for %s (stack %s, service %s): manifest lookup for candidate "
                    "%r failed, skipping: %s",
                    purpose,
                    image_ref,
                    stack_name,
                    service_name,
                    tag,
                    err,
                )
                continue
            if digest == target_digest:
                return tag

        return None
