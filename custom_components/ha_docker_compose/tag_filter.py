"""Registry tag-list filtering and newest-version selection — see
REGISTRY_TAG_WALK_SPEC.md.

Pure logic, zero network calls, zero HA imports: registry_client.py fetches
the raw tag list, update_coordinator.py calls this module to turn it into
"the newest real version tag" (or nothing).

Deliberately conservative throughout: prefer under-matching (missing a real
version) over over-matching (falsely treating a non-version tag as one) —
per the spec, a false "newer version found" is actively misleading in a way
"nothing found" is not. No semver-severity classification lives here or
anywhere else in this integration — this module only ever answers "what's
the newest real version tag", never "how big a jump is this" (see WUD's own
tracker for why that's out of scope: multi-segment version+build-number
tags getting misordered, arch-suffixed tags misread as versions, projects
changing versioning schemes over time — all real, persistent bugs in a
mature tool that does attempt severity classification).
"""
from __future__ import annotations

import logging
import re

from packaging.version import InvalidVersion, Version

from .version_detect import NON_VERSION_TAGS, has_version_structure, is_version_tag

_LOGGER = logging.getLogger(__name__)

# Optional per-service compose `labels:` overrides — same spirit as the
# GitHub-source label override (oci_labels.py/github_coordinator.py):
# independent, optional, additive escape hatches for when the default
# filtering doesn't handle a particular repository's tag scheme well.
TAG_INCLUDE_LABEL = "ha_docker_compose.tag_include"
TAG_EXCLUDE_LABEL = "ha_docker_compose.tag_exclude"

# Bare architecture/platform tokens seen in real-world multi-arch image tag
# lists (e.g. a project that publishes both "1.2.3" and per-arch tags like
# "amd64" or "arm64v8" for the same manifest list). Most of these start
# with a letter so version_detect.is_version_tag() already rejects them on
# its own digit-leading check; this exists for the few that don't (e.g.
# "386") and for combinations of these with other non-version words (e.g.
# "amd64-latest").
_ARCH_TOKENS = {
    "amd64",
    "arm64",
    "arm",
    "armv6",
    "armv7",
    "arm32v6",
    "arm32v7",
    "arm64v8",
    "i386",
    "386",
    "x86",
    "x86_64",
    "ppc64le",
    "s390x",
    "riscv64",
    "aarch64",
}

_SPLIT_RE = re.compile(r"[-_]")

# The dot-separated-digit-structure requirement (has_version_structure())
# now lives in version_detect.py, not here — it's shared with
# detect_version()'s OCI-label validation (UNVERIFIED_DETECTED_VERSION_SPEC.md's
# sibling hardening spec), and version_detect.py already sits below this
# module in the import graph (tag_filter.py already imports
# NON_VERSION_TAGS/is_version_tag from it), so this is the direction that
# avoids a circular import. Originally added here to prevent
# ghcr.io/esphome/esphome's per-commit git-hash tags (e.g. "9208227", a
# hash composed entirely of digits — a perfectly "valid" if absurd
# single-component PEP 440 version) from winning the newest-tag
# comparison against every real release.

# One-time, unconditional (not gated behind debug logging) fingerprint of
# exactly which file and regex got loaded, logged once at import time —
# added specifically to settle "is the deployed file actually this
# version" without guesswork, the same way this integration's action
# entities log an unconditional breadcrumb on their first line for the
# identical reason (editing a custom component's .py files needs a full
# HA restart to be picked up at all; a stale copy of this exact file —
# new as of the registry-tag-walk feature, easy for a partial/selective
# deploy to miss even when every previously-existing file gets updated —
# is a real, previously-confirmed failure mode for this integration, not
# a hypothetical one). If this line is absent from the log after a full
# restart, the deployed tag_filter.py predates this fix, full stop — no
# need to keep guessing at caching/monkeypatching explanations.
_LOGGER.info("tag_filter loaded from %s", __file__)


def _is_noise_tag(tag: str) -> bool:
    """True for a tag built entirely out of arch tokens and/or known
    floating-tag words (e.g. "386", "amd64-latest") — noise that could
    otherwise slip past is_version_tag()'s digit-leading check."""
    parts = [part for part in _SPLIT_RE.split(tag.lower()) if part]
    return bool(parts) and all(part in _ARCH_TOKENS or part in NON_VERSION_TAGS for part in parts)


def is_real_version_tag(tag: str) -> bool:
    """Default (no per-service override) real-version-tag filter."""
    return is_version_tag(tag) and has_version_structure(tag) and not _is_noise_tag(tag)


def filter_tags(
    tags: list[str], *, tag_include: str | None = None, tag_exclude: str | None = None
) -> list[str]:
    """Reduce a registry's raw tag list to real-version candidates.

    tag_include, when set, REPLACES the default is_real_version_tag()
    shape check for the initial candidate set rather than narrowing it
    further — the whole point of an include pattern is to handle a tag
    scheme the default digit-leading check can't (e.g. "release-4.2.1"),
    so requiring tags to also pass the default check first would defeat
    the feature for exactly the case it exists to solve. tag_exclude
    always applies last, removing matches from whichever candidate set
    (include-based or default) was selected.

    An invalid regex in either override is logged and ignored (falls back
    to the default filter / no exclusion) rather than raising — a bad
    label value must not break update checking for the whole service.
    """
    if tag_include:
        try:
            include_re = re.compile(tag_include)
        except re.error as err:
            _LOGGER.warning(
                "Invalid %s regex %r: %s — ignoring override, using default filtering",
                TAG_INCLUDE_LABEL,
                tag_include,
                err,
            )
            candidates = [t for t in tags if is_real_version_tag(t)]
        else:
            candidates = [t for t in tags if include_re.search(t)]
    else:
        candidates = [t for t in tags if is_real_version_tag(t)]

    if tag_exclude:
        try:
            exclude_re = re.compile(tag_exclude)
        except re.error as err:
            _LOGGER.warning(
                "Invalid %s regex %r: %s — ignoring override",
                TAG_EXCLUDE_LABEL,
                tag_exclude,
                err,
            )
        else:
            candidates = [t for t in candidates if not exclude_re.search(t)]

    return candidates


# Curated, deliberately narrow — Docker base-image/distro identifiers
# only, the way they commonly appear as a tag's trailing `-<word>` (e.g.
# "2.1.2-alpine"). NOT "any word packaging.version.Version doesn't
# recognize" (confirmed real case for why that would be unsafe: a tag
# suffixed "-nightly" or "-snapshot" would also fail to parse directly,
# and blindly stripping *any* unrecognized suffix would wrongly promote
# an unstable/non-final build to "latest final release" for
# select_newest_version_tag(), which has no verification step to catch
# it — unlike pull_target_version/verified_current_version, where a
# wrong strip only costs an extra, ultimately-failing digest lookup, not
# a wrong answer). Confirmed none of these collide with packaging's own
# recognized pre/post/dev-release keywords (alpha/a/beta/b/preview/pre/
# c/rc/post/rev/r/dev) — direct test, not assumed.
#
# Real, confirmed case this exists for: eclipse-mosquitto publishes its
# 2.1.x line *only* as "2.1.2-alpine" etc. — no bare "2.1.2" tag exists
# at all (confirmed live against Docker Hub) — while its older 1.x/2.0.x
# lines publish both a bare tag and a "-openssl" variant of the exact
# same version. Scoped to Linux base images only (this integration's own
# domain); Windows-container base tags (nanoserver, windowsservercore)
# deliberately left out.
KNOWN_BASE_IMAGE_SUFFIXES = frozenset(
    {
        "alpine",
        "slim",
        "debian",
        "ubuntu",
        "distroless",
        "bookworm",
        "bullseye",
        "buster",
        "stretch",
        "trixie",
        "focal",
        "jammy",
        "noble",
        "bionic",
        "xenial",
    }
)


def _strip_known_base_image_suffix(tag: str) -> str | None:
    """If `tag` doesn't parse directly but is `<version>-<suffix>` where
    `suffix` (case-insensitive) is a known base-image/distro identifier
    and `<version>` parses cleanly on its own, return `<version>`;
    otherwise None. Only ever called as a fallback after a direct
    Version(tag) has already failed — see rank_final_versions().

    Single trailing suffix only (rpartition on the last "-"): a tag like
    "3.12-slim-bookworm" (two stacked suffixes, a real, common pattern —
    e.g. Python's own official images) won't match and stays excluded,
    same as before this function existed. That's a coverage gap, not a
    correctness risk — this module prefers under-matching over
    over-matching throughout (see module docstring), and extending this
    to strip multiple stacked suffixes wasn't needed for any confirmed
    real case yet.
    """
    prefix, sep, suffix = tag.rpartition("-")
    if not sep or suffix.lower() not in KNOWN_BASE_IMAGE_SUFFIXES:
        return None
    try:
        Version(prefix)
    except InvalidVersion:
        return None
    return prefix


def rank_final_versions(tags: list[str]) -> list[str]:
    """Final-release candidates only (dev releases and pre-releases both
    excluded — see select_newest_version_tag()'s docstring for why),
    newest first. Version-aware (packaging.version.Version, a small
    mature library rather than a hand-rolled comparator — chosen after
    reviewing WUD's own version-comparison bug history), not
    lexicographic — 2.9.10 correctly sorts ahead of 2.9.9. A tag that
    doesn't parse as a version at all (InvalidVersion) is silently
    skipped, not treated as an error: expected for some tag lists even
    after filter_tags() — *unless* it's `<version>-<known base-image
    suffix>` (e.g. "2.1.2-alpine"), in which case the version prefix is
    used for ranking instead (see _strip_known_base_image_suffix()) —
    the *original* tag string is still what's returned, never a
    synthesized bare version, since that string is what a caller needs
    to actually look the tag up on the registry (see REGISTRY_TAG_WALK_SPEC.md's
    base-image-suffix amendment).

    Tie-break for tags that compare as the exact same Version (e.g. "2.9"
    and "2.9.0" — packaging.version.Version("2.9") == Version("2.9.0"),
    confirmed: release-segment comparison pads the shorter tuple with
    zeros, so these are equal, not just close): the tag with more
    dot-separated release segments wins — "2.9.0" over "2.9" — since it's
    the more specific/complete form. Without this, a plain sort is
    stable, so the winner would silently depend on the registry's own
    tag-list ordering (confirmed by direct test: swapping the input order
    of two Version-equal tags changed which one sorted first). This only
    ever breaks ties between tags that are already equal as real
    versions; it never lets a less-specific tag outrank a genuinely newer
    one (2.10 still beats 2.9.0 regardless of segment count).

    The single ranking rule shared by select_newest_version_tag() (takes
    just the top result) and tag_walk_coordinator.py's pull-target-version
    and verified-current-version searches (each takes the top N for a
    bounded digest-lookup search, per REGISTRY_TAG_WALK_SPEC.md's "Cost
    consideration" — reusing this function is the "consistent behavior
    already established" tie-break the spec asks for, rather than a
    second hand-rolled rule).
    """
    finals: list[tuple[Version, str]] = []
    for t in tags:
        try:
            version = Version(t)
        except InvalidVersion:
            stripped = _strip_known_base_image_suffix(t)
            if stripped is None:
                _LOGGER.debug("rank_final_versions: %r doesn't parse as a version, skipping", t)
                continue
            version = Version(stripped)
            _LOGGER.debug(
                "rank_final_versions: %r doesn't parse directly, but stripping its known "
                "base-image suffix gives %r, which does — ranking %r as %s",
                t,
                stripped,
                t,
                version,
            )
        if version.is_devrelease:
            _LOGGER.debug(
                "rank_final_versions: %r is a dev release, excluding from consideration", t
            )
            continue
        if version.is_prerelease:
            _LOGGER.debug(
                "rank_final_versions: %r is a pre-release, excluding from consideration", t
            )
            continue
        finals.append((version, t))

    # (version, segment count) — reverse=True descends both, so among
    # Version-equal tags the one with more release segments (more
    # specific) sorts first. See docstring above.
    finals.sort(key=lambda pair: (pair[0], len(pair[0].release)), reverse=True)
    return [t for _, t in finals]


def select_newest_version_tag(tags: list[str]) -> str | None:
    """The newest final release among tags, or None if none exists.

    Dev releases (Version.is_devrelease) and pre-releases
    (Version.is_prerelease — alpha/beta/rc) are both excluded from
    consideration entirely — never shown as "latest" under any
    circumstance, even if that means returning an older final release
    than the actual newest tag on the registry (e.g. 2026.8.2 instead of
    2026.9.0b4, if 2026.9.0 final hasn't shipped yet), or returning
    nothing at all if no final release exists in the candidate set yet.
    An earlier version of this function fell back to the newest
    pre-release of the newest release line when no final existed for that
    line yet (confirmed working as designed against ghcr.io/esphome/
    esphome, which had no 2026.9.0 final at the time) — deliberately
    reverted: a pre-release surfacing as "latest" at all was decided to be
    the wrong default for this sensor, regardless of how it's scoped. See
    REGISTRY_TAG_WALK_SPEC.md's second amendment. Consistent with this
    module's existing "nothing found" beats "a potentially misleading
    result" principle.
    """
    ranked = rank_final_versions(tags)
    return ranked[0] if ranked else None


def is_not_behind(pinned_digest: str | None, local_digest: str | None) -> bool:
    """True only if both digests are known and identical — i.e. the
    running container's local image already matches what the pinned
    (possibly floating) tag currently resolves to on the registry.

    Used by tag_walk_coordinator.py to decide whether
    pull_target_version's own digest search can be reused as
    verified_current_version's answer too (same target digest, so the
    same match), instead of paying for a second bounded search against
    local_digest. Either digest missing (no digest check has completed
    yet) is treated as "can't tell, don't assume not-behind" — False,
    not an exception — so the caller falls through to its own search
    rather than wrongly reusing a result that was never actually
    verified against local_digest.
    """
    return bool(pinned_digest) and bool(local_digest) and pinned_digest == local_digest
