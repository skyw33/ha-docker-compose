"""Persisted per-service pull/digest history.

Uses HA's Store (JSON under .storage/, survives restarts) rather than the
coordinator's in-memory data — this is the audit trail of what's actually
been pulled and run, especially valuable for floating tags where the tag
itself never changes. Written on every successful pull; never used to
compute "latest available version" (see PROJECT_SPEC.md's explicit
non-goal on that).

Also carries `assumed_version` — an unverified snapshot of
latest_registry_tag at the moment of a successful pull, used only as
detected_version's last-resort fallback (see
UNVERIFIED_DETECTED_VERSION_SPEC.md) when nothing else in that chain
resolves. Distinct from the "latest available version" non-goal above:
this records what a floating tag's pull *probably* landed on at the time,
never claims to know what's currently available on the registry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


class DigestHistoryStore:
    """One instance per config entry, keyed `{stack: {service: record}}`."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_digest_history")
        self._data: dict[str, dict[str, Any]] | None = None

    async def async_load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            self._data = await self._store.async_load() or {}
        return self._data

    def get(self, stack: str, service: str) -> dict[str, Any] | None:
        return (self._data or {}).get(stack, {}).get(service)

    async def async_record_pull(
        self,
        stack: str,
        service: str,
        tag: str,
        digest: str,
        assumed_version: str | None = None,
    ) -> None:
        """assumed_version: whatever latest_registry_tag resolved to at
        the moment of this successful pull — see
        UNVERIFIED_DETECTED_VERSION_SPEC.md. An unverified, last-resort
        source for detected_version's fallback chain, distinct from
        `tag` above (the pinned tag itself, always known) and from
        `digest` (a verified fact). If this particular pull's
        latest_registry_tag wasn't available (None), the previously
        recorded assumed_version for this service is preserved rather
        than cleared — same "don't overwrite known-good data with
        unknown" principle already applied to remote_digest elsewhere in
        this integration (update_coordinator.py keeps the last-known
        remote_digest across a transient registry failure rather than
        resetting it)."""
        data = await self.async_load()
        stack_data = data.setdefault(stack, {})
        previous_record = stack_data.get(service, {})
        previous_digest = previous_record.get("digest")
        resolved_assumed_version = (
            assumed_version if assumed_version is not None else previous_record.get("assumed_version")
        )
        stack_data[service] = {
            "tag": tag,
            "digest": digest,
            "pulled_at": datetime.now(timezone.utc).isoformat(),
            "previous_digest": previous_digest,
            "assumed_version": resolved_assumed_version,
        }
        await self._store.async_save(data)
