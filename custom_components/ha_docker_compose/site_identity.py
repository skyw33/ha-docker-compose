"""Pure slug/uniqueness logic for per-entry site names — see
MULTI_SITE_IDENTITY_SPEC.md. Kept dependency-light (no homeassistant
imports), same rationale as device_ids.py: whether a site name collides
with another entry's needs to be unit-testable without a running Home
Assistant instance.
"""
from __future__ import annotations

import re

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


def slugify_site_name(name: str) -> str:
    """Lowercase, each run of non-alphanumeric characters collapsed to a
    single underscore, leading/trailing underscores stripped. Falls back
    to "site" for a name with no alphanumeric characters at all (e.g. an
    empty string), so a resolved slug is never blank."""
    slug = _SLUG_INVALID_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "site"


def resolve_unique_site_slug(desired_slug: str, taken_slugs: set[str]) -> str:
    """Auto-suffixes `desired_slug` with _2, _3, ... until it no longer
    collides with anything in `taken_slugs`. Returns it unchanged if
    already free.

    Used for the auto-derived default only — an explicitly user-typed
    name that collides is rejected by the caller instead of being
    silently suffixed (see MULTI_SITE_IDENTITY_SPEC.md's collision-safety
    requirement: only an explicit duplicate is ever rejected)."""
    if desired_slug not in taken_slugs:
        return desired_slug
    suffix = 2
    while f"{desired_slug}_{suffix}" in taken_slugs:
        suffix += 1
    return f"{desired_slug}_{suffix}"


def other_site_slugs(
    entries: list[tuple[str, str | None]], exclude_entry_id: str | None
) -> set[str]:
    """`entries` is (entry_id, resolved_site_slug_or_None) for every
    ha_docker_compose config entry currently known to hass. Returns the
    slugs already claimed by entries other than `exclude_entry_id` (pass
    None when the flow-in-progress entry doesn't exist yet, i.e. initial
    setup rather than a reconfigure of an existing one)."""
    return {slug for entry_id, slug in entries if entry_id != exclude_entry_id and slug}
