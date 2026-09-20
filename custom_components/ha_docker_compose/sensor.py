"""Sensor entities: stack state, raw (un-substituted) compose file text —
see RAW_COMPOSE_CONFIG_SPEC.md, deliberately not the `.env`-resolved
config, which could contain a real secret value — per-service live
state/stats, per-service update-digest bookkeeping (running digest,
last-pulled timestamp — the binary_sensor.*_update_available entity itself
lives in binary_sensor.py), the log-command helper, the best-effort
latest-GitHub-release lookup, and local version detection (label/tag,
zero network calls — see LOCAL_VERSION_DETECTION_SPEC.md).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STACK_STATE_UPDATING
from .coordinator import LogFetchResult, PullError, StacksCoordinator
from .engine import ContainerInfo
from .entity import StackDeviceEntity, service_attributes, service_device_info
from .github_coordinator import GitHubReleaseCoordinator, ServiceMetadata
from .github_release import GitHubRelease
from .tag_walk_coordinator import ServiceTagWalkStatus, TagWalkCoordinator
from .update_coordinator import ServiceUpdateStatus, UpdateCheckCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: StacksCoordinator = data["coordinator"]
    update_coordinator: UpdateCheckCoordinator = data["update_coordinator"]
    tag_walk_coordinator: TagWalkCoordinator = data["tag_walk_coordinator"]
    github_coordinator: GitHubReleaseCoordinator = data["github_coordinator"]

    entities: list[SensorEntity] = []
    for stack in coordinator.stacks:
        entities.append(StackStateSensor(coordinator, entry.entry_id, stack.name))
        entities.append(StackComposeConfigSensor(coordinator, entry.entry_id, stack.name))
        for service in stack.service_names:
            entities.append(ServiceStateSensor(coordinator, entry.entry_id, stack.name, service))
            entities.append(ServiceCpuSensor(coordinator, entry.entry_id, stack.name, service))
            entities.append(ServiceMemorySensor(coordinator, entry.entry_id, stack.name, service))
            entities.append(ServiceUptimeSensor(coordinator, entry.entry_id, stack.name, service))
            entities.append(ServiceLogCommandSensor(coordinator, entry.entry_id, stack.name, service))
            entities.append(
                ServiceLastFetchedLogsSensor(coordinator, entry.entry_id, stack.name, service)
            )
            entities.append(
                ServiceRunningDigestSensor(update_coordinator, entry.entry_id, stack.name, service)
            )
            entities.append(
                ServiceLastPulledSensor(update_coordinator, entry.entry_id, stack.name, service)
            )
            entities.append(
                ServiceLatestRegistryTagSensor(
                    tag_walk_coordinator, entry.entry_id, stack.name, service
                )
            )
            entities.append(
                ServicePullTargetVersionSensor(
                    tag_walk_coordinator, entry.entry_id, stack.name, service
                )
            )
            entities.append(
                ServiceLatestGithubReleaseSensor(
                    github_coordinator, entry.entry_id, stack.name, service
                )
            )
            entities.append(
                ServiceDetectedVersionSensor(github_coordinator, entry.entry_id, stack.name, service)
            )

    async_add_entities(entities)


class StackStateSensor(StackDeviceEntity, SensorEntity):
    _attr_icon = "mdi:layers-outline"

    def __init__(self, coordinator: StacksCoordinator, entry_id: str, stack_name: str) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_state"
        self._attr_name = "State"

    @property
    def native_value(self) -> str | None:
        # Takes precedence over the real container-derived state for
        # exactly the duration of an in-progress Pull update — set/cleared
        # by StackPullUpdateButton (button.py), not by this coordinator's
        # own poll, so a fast-poll cycle landing mid-pull can't overwrite
        # it back to the (momentarily stale) pre-pull state.
        if self._stack_name in self.coordinator.pulling_stacks:
            return STACK_STATE_UPDATING
        status = self._status
        return status.state if status else None

    @property
    def _pull_error(self) -> PullError | None:
        return self.coordinator.last_pull_errors.get(self._stack_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        attrs: dict[str, Any] = {"has_env_file": status.info.has_env_file} if status else {}
        # Surfaces a failed Pull update once the transient "updating" state
        # above has already reverted — without this, a real, logged
        # failure (bad tag, registry rate limit, sidecar lost track of the
        # exec, ...) looked identical to a success: state just quietly went
        # back to "running"/"stopped" with nothing to tell them apart. See
        # PullError / PULL_ERROR_VISIBILITY_SPEC.md. Cleared by the next
        # Pull press, not by time or by a later successful poll.
        pull_error = self._pull_error
        if pull_error is not None:
            attrs["last_pull_error"] = pull_error.reason
            attrs["last_pull_error_step"] = pull_error.step
            attrs["last_pull_error_at"] = pull_error.failed_at.isoformat()
        return attrs


class StackComposeConfigSensor(StackDeviceEntity, SensorEntity):
    """State is a cheap summary; the compose file's raw, unparsed,
    un-substituted on-disk text lives in the `compose_config` attribute
    (same attribute key as before, so an existing dashboard reading it
    doesn't need to change its reference — only the value's shape does).

    Deliberately NOT the resolved config from `docker compose config`
    anymore — see RAW_COMPOSE_CONFIG_SPEC.md. That version resolved `.env`
    substitution, meaning any secret referenced via `${VAR}` (e.g.
    SESSION_SECRET) appeared as its real value in this entity's attribute
    — the only place in this integration a real secret could end up
    visible in HA. This is why it was disabled by default from the start.
    A plain file read (discovery.py's StackInfo.raw_compose_text, read
    once at discovery/reload time — no exec, no sidecar involvement) can
    never contain a resolved secret, since `${VAR}` is never substituted:
    it stays literal. Safe to enable by default. `docker compose config`
    is still run at discovery time for other, genuinely internal uses
    (protection-label parsing, image-ref extraction, service enumeration)
    — StackInfo.compose_config, unaffected — just no longer surfaced here
    for display.
    """

    _attr_icon = "mdi:file-code-outline"
    # No longer secret-risk-gated — see class docstring. Explicit True
    # (HA's own default when unset) purely so the "this used to be False
    # for a real reason, and that reason no longer applies" history stays
    # visible in the diff/code rather than silently vanishing.
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: StacksCoordinator, entry_id: str, stack_name: str) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_compose_config"
        self._attr_name = "Compose config"

    @property
    def native_value(self) -> str | None:
        status = self._status
        if status is None:
            return None
        return f"{len(status.info.service_names)} services"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        if status is None:
            return {}
        return {"compose_config": status.info.raw_compose_text}


class _ServiceEntity(StackDeviceEntity):
    def __init__(
        self, coordinator: StacksCoordinator, entry_id: str, stack_name: str, service_name: str
    ) -> None:
        super().__init__(coordinator, entry_id, stack_name)
        self._service_name = service_name
        # Own device (not the stack's) so a service dropped from an edited
        # compose file can be pruned independently on reload — see
        # entity.py and __init__.py's stale-device cleanup.
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _container(self) -> ContainerInfo | None:
        status = self._status
        if status is None:
            return None
        return status.container_for_service(self._service_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # See SERVICE_ATTRIBUTES_SPEC.md. Base implementation — a
        # subclass that adds its own attributes merges into this rather
        # than replacing it (see e.g. ServiceStateSensor below).
        return service_attributes(self._stack_name, self._service_name)


class ServiceStateSensor(_ServiceEntity, SensorEntity):
    _attr_icon = "mdi:docker"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_state"
        self._attr_name = f"{service_name} state"

    @property
    def native_value(self) -> str:
        container = self._container
        # A service defined in compose but never started has no container at
        # all yet — distinct from "exited", which means it ran and stopped.
        return container.state if container else "not_created"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        container = self._container
        if container is not None:
            attrs.update({"health": container.health, "container_name": container.name})
        return attrs


class ServiceCpuSensor(_ServiceEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_cpu"
        self._attr_name = f"{service_name} CPU"

    @property
    def native_value(self) -> float | None:
        container = self._container
        if container is None or container.cpu_percent is None:
            return None
        return round(container.cpu_percent, 1)


class ServiceMemorySensor(_ServiceEntity, SensorEntity):
    """Displayed in MB — the underlying Engine API data (memory_usage_bytes)
    stays in bytes at the source; only this sensor's display value/unit is
    converted, for readability."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:memory"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_memory"
        self._attr_name = f"{service_name} memory"

    @property
    def native_value(self) -> float | None:
        container = self._container
        if container is None or container.memory_usage_bytes is None:
            return None
        return round(container.memory_usage_bytes / (1024 * 1024), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        container = self._container
        if container is not None and container.memory_limit_bytes is not None:
            attrs["limit_bytes"] = container.memory_limit_bytes
        return attrs


class ServiceUptimeSensor(_ServiceEntity, SensorEntity):
    """Reports container start time as a timestamp (not elapsed duration),
    so it updates itself relatively in the UI without needing a poll."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_uptime"
        self._attr_name = f"{service_name} started at"

    @property
    def native_value(self):
        container = self._container
        return container.started_at if container else None


class ServiceLogCommandSensor(_ServiceEntity, SensorEntity):
    """Literal copy-pasteable `docker logs` command, built from the real
    container name already known from the Engine API poll — the deliberate
    scope cut vs. true live log streaming (see PROJECT_SPEC.md)."""

    _attr_icon = "mdi:console-line"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_log_command"
        self._attr_name = f"{service_name} log command"

    @property
    def native_value(self) -> str | None:
        container = self._container
        if container is None:
            return None
        return f"docker logs -f {container.name} --tail 200"


class ServiceLastFetchedLogsSensor(_ServiceEntity, SensorEntity):
    """State is the timestamp of the most recent Fetch logs button press
    for this service; the actual fetched content lives in the `log_text`
    attribute (same pattern as ServiceLastPulledSensor's `tag`/
    `previous_digest`). See FETCH_LOGS_ATTRIBUTE_SPEC.md — this replaces
    the original LOGS_BUTTON_SPEC.md's `_LOGGER.info` output, which HA's
    own Logs page never actually surfaced (INFO is below its default
    WARNING floor).

    Created unconditionally for every service, same as every other sensor
    in this file — read-only entities aren't gated by
    `ha_docker_compose.protection: full` (see protection.py); only
    switch.py/button.py's action entities are.

    Bound to the fast StacksCoordinator (not the update-check one) purely
    because that's the coordinator ServiceFetchLogsButton already has a
    reference to — this data has nothing to do with container stats
    polling, it's just where the button's coordinator.async_update_listeners()
    call lands. Each press overwrites the previous result in place: a
    point-in-time snapshot, not accumulating history."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:text-box-search-outline"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_last_fetched_logs"
        self._attr_name = f"{service_name} last fetched logs"

    @property
    def _result(self) -> LogFetchResult | None:
        return self.coordinator.last_fetched_logs.get((self._stack_name, self._service_name))

    @property
    def native_value(self):
        result = self._result
        return result.fetched_at if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        result = self._result
        if result is not None:
            attrs["log_text"] = result.log_text
        return attrs


class _UpdateServiceEntity(CoordinatorEntity[UpdateCheckCoordinator]):
    """Base for entities sourced from the slow update-check coordinator
    rather than the fast stats one — device_info is built the same way as
    _ServiceEntity's (see entity.py) even though this platform is bound to
    a different coordinator (see binary_sensor.py, which has the same
    split)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UpdateCheckCoordinator,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._service_name = service_name
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _update_status(self) -> ServiceUpdateStatus | None:
        summary = (self.coordinator.data or {}).get(self._stack_name)
        return summary.services.get(self._service_name) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # See SERVICE_ATTRIBUTES_SPEC.md. Base implementation, merged
        # into by a subclass that adds its own attributes.
        return service_attributes(self._stack_name, self._service_name)


class ServiceRunningDigestSensor(_UpdateServiceEntity, SensorEntity):
    """Short hash as the state; full digest as an attribute."""

    _attr_icon = "mdi:pound-box-outline"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_running_digest"
        self._attr_name = f"{service_name} running digest"

    @property
    def native_value(self) -> str | None:
        status = self._update_status
        if status is None or not status.local_digest:
            return None
        return status.local_digest.split(":")[-1][:12]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        status = self._update_status
        if status is not None:
            attrs.update({"full_digest": status.local_digest, "image": status.image})
        return attrs


class ServiceLastPulledSensor(_UpdateServiceEntity, SensorEntity):
    """Timestamp from the persisted digest-pull history (storage.py), not
    from the update-check poll itself."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:history"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_last_pulled"
        self._attr_name = f"{service_name} last pulled"

    @property
    def native_value(self):
        status = self._update_status
        if status is None:
            _LOGGER.debug(
                "%s: no ServiceUpdateStatus for stack '%s' service '%s' yet — native_value is None",
                self.entity_id,
                self._stack_name,
                self._service_name,
            )
            return None
        if not status.last_pulled:
            _LOGGER.debug(
                "%s: ServiceUpdateStatus.last_pulled is empty for stack '%s' service '%s' — "
                "native_value is None (check update_coordinator debug logs for what "
                "_check_one() actually read from digest history)",
                self.entity_id,
                self._stack_name,
                self._service_name,
            )
            return None
        parsed = dt_util.parse_datetime(status.last_pulled)
        if parsed is None:
            _LOGGER.warning(
                "%s: could not parse last_pulled value %r as a datetime for stack '%s' "
                "service '%s'",
                self.entity_id,
                status.last_pulled,
                self._stack_name,
                self._service_name,
            )
        return parsed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        status = self._update_status
        if status is not None:
            attrs.update({"tag": status.tag, "previous_digest": status.previous_digest})
        return attrs


class _TagWalkServiceEntity(CoordinatorEntity[TagWalkCoordinator]):
    """Base for entities sourced from the slow tag-walk coordinator
    (registry tag-list fetch — latest_registry_tag, pull_target_version)
    — same device_info construction as _ServiceEntity's/
    _UpdateServiceEntity's, on this integration's third independent
    coordinator (see tag_walk_coordinator.py and
    REGISTRY_TAG_WALK_SPEC.md's "Prerequisite" amendment for why this
    isn't just bound to UpdateCheckCoordinator like it used to be)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TagWalkCoordinator,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._service_name = service_name
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _tag_walk_status(self) -> ServiceTagWalkStatus | None:
        return (self.coordinator.data or {}).get(self._stack_name, {}).get(self._service_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # See SERVICE_ATTRIBUTES_SPEC.md.
        return service_attributes(self._stack_name, self._service_name)


class ServiceLatestRegistryTagSensor(_TagWalkServiceEntity, SensorEntity):
    """Newest real version tag found by walking the registry's full tag
    list (registry_client.py/tag_filter.py) — see
    REGISTRY_TAG_WALK_SPEC.md. A third, separate signal alongside
    detected_version (local tag/label, zero network calls) and
    latest_github_release (GitHub's own release list, a different data
    source entirely) — none of the three imply or corroborate each other.
    Informational only: if this differs from detected_version, that's not
    an automatic "update available" claim — update_available (digest
    comparison) is unchanged and remains the authoritative signal for
    that."""

    _attr_icon = "mdi:tag-multiple-outline"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_latest_registry_tag"
        self._attr_name = f"{service_name} latest registry tag"

    @property
    def native_value(self) -> str | None:
        status = self._tag_walk_status
        return status.latest_registry_tag if status else None


class ServicePullTargetVersionSensor(_TagWalkServiceEntity, SensorEntity):
    """Which published version tag, if any, currently shares the exact
    same digest as this service's own pinned tag right now — i.e. "if I
    pull right now, what version would I actually end up running". See
    REGISTRY_TAG_WALK_SPEC.md's "Pull-target version" spec.

    Distinct from both update_available (digest-only, says nothing about
    which version a pull would land on) and latest_registry_tag (the
    project's overall newest release, independent of what's pinned) — a
    service pinned to `latest`/`stable` may resolve to a version that
    lags behind latest_registry_tag; a service already pinned to a
    specific version tag should show that same version here (pulling
    would be a no-op). Empty/unknown if no candidate tag's digest matches
    within the bounded search this performs (tag_walk_coordinator.py) —
    a real, expected outcome for some projects, never a guess."""

    _attr_icon = "mdi:target"

    def __init__(self, coordinator, entry_id, stack_name, service_name) -> None:
        super().__init__(coordinator, entry_id, stack_name, service_name)
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_pull_target_version"
        self._attr_name = f"{service_name} pull target version"

    @property
    def native_value(self) -> str | None:
        status = self._tag_walk_status
        return status.pull_target_version if status else None


class ServiceLatestGithubReleaseSensor(CoordinatorEntity[GitHubReleaseCoordinator], SensorEntity):
    """Best-effort, may be empty/unavailable — see PROJECT_SPEC.md's
    explicit non-goal: this is never linked to update-available or to what
    is currently running, only presented as separate information."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:github"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: GitHubReleaseCoordinator,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._service_name = service_name
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_latest_github_release"
        self._attr_name = f"{service_name} latest GitHub release"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def _metadata(self) -> ServiceMetadata | None:
        return (self.coordinator.data or {}).get(self._stack_name, {}).get(self._service_name)

    @property
    def _release(self) -> GitHubRelease | None:
        metadata = self._metadata
        return metadata.release if metadata else None

    @property
    def native_value(self) -> str | None:
        release = self._release
        return release.tag_name if release else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # stack/service (SERVICE_ATTRIBUTES_SPEC.md) are always present.
        attrs = service_attributes(self._stack_name, self._service_name)
        release = self._release
        if release is None:
            return attrs
        # release_notes/release_url are zero-cost by construction — both
        # come from the same /releases/latest response already fetched for
        # native_value, no additional GitHub API call. release_notes is
        # raw markdown, verbatim, no length limit; None if GitHub has no
        # notes for this release. See RELEASE_NOTES_SPEC.md.
        attrs.update(
            {
                "published_at": release.published_at,
                "release_url": release.html_url,
                "release_notes": release.body,
            }
        )
        return attrs


class ServiceDetectedVersionSensor(CoordinatorEntity[GitHubReleaseCoordinator], SensorEntity):
    """Local version detection (label or image tag) — zero network calls.
    A third, independent signal alongside update_available (digest-only)
    and latest_github_release (GitHub API); none of the three imply or
    corroborate each other. See LOCAL_VERSION_DETECTION_SPEC.md."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:tag-outline"

    def __init__(
        self,
        coordinator: GitHubReleaseCoordinator,
        entry_id: str,
        stack_name: str,
        service_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stack_name = stack_name
        self._service_name = service_name
        self._attr_unique_id = f"{entry_id}_{stack_name}_{service_name}_detected_version"
        self._attr_name = f"{service_name} detected version"
        self._attr_device_info = service_device_info(entry_id, stack_name, service_name)

    @property
    def native_value(self) -> str | None:
        metadata = (self.coordinator.data or {}).get(self._stack_name, {}).get(self._service_name)
        return metadata.detected_version if metadata else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # See SERVICE_ATTRIBUTES_SPEC.md.
        return service_attributes(self._stack_name, self._service_name)
