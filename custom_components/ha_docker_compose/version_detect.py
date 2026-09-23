"""Local version detection: label or tag, zero network calls.

A third, independent signal alongside `update_available` (digest-only,
engine.py/registry_client.py) and `latest_github_release` (GitHub API,
github_coordinator.py) — see LOCAL_VERSION_DETECTION_SPEC.md. Explicitly
"detect and display what we already know," not a comparison/outdated
check: no new network calls, no relationship implied between this and the
other two signals.

Extended with one final, strictly-last-resort fallback — a persisted,
unverified assumption from the last successful pull's latest_registry_tag
— see UNVERIFIED_DETECTED_VERSION_SPEC.md. Still no I/O in this module
itself: the caller (github_coordinator.py) is responsible for reading the
persisted value and passing it in.

The OCI version label is validated structurally (has_version_structure(),
below) before being trusted, not against a fixed word list — confirmed
two real-world non-version placeholder values self-reported by an image's
own label (gatus: "latest"; onstar2mqtt: "weekly", a release-cadence
name), a category of bug a word list only ever catches retroactively, one
word at a time.
"""
from __future__ import annotations

import logging
import re

from .image_ref import tag as extract_tag
from .oci_labels import resolve_label

_LOGGER = logging.getLogger(__name__)

OCI_VERSION_LABEL = "org.opencontainers.image.version"

NON_VERSION_TAGS = {"latest", "stable", "edge", "beta", "dev", "main", "master", "nightly"}

_LOOKS_LIKE_VERSION_RE = re.compile(r"^v?\d")
_LEADING_V_RE = re.compile(r"^[vV]\d")

# Requires at least two dot-separated numeric components (e.g. "2.9" or
# "2026.9.1") — a strictly stronger bar than is_version_tag()'s plain
# digit-leading check above. Originally added in tag_filter.py to close a
# registry-tag-walking bug (a git commit hash tag composed entirely of
# digits, e.g. "9208227", passed the weaker digit-leading check and won
# the newest-tag comparison against every real release) and moved here,
# made public, so detect_version()'s OCI-label check can reuse the same
# structural validation — see the module docstring's note on the
# `org.opencontainers.image.version` label validation hardening below.
# Confirmed second real-world case of the same underlying category of bug
# (a non-version placeholder value slipping past a word-list-only check):
# onstar2mqtt's image sets this label to the literal string "weekly" (a
# release-cadence name, not a version) — rather than adding "weekly" to
# NON_VERSION_TAGS one word at a time, requiring actual version shape
# closes the whole category of "some non-version word we haven't seen
# yet" at once. tag_filter.py imports this rather than redefining its own
# copy — one structural-validation rule, not two to keep in sync.
_VERSION_STRUCTURE_RE = re.compile(r"^v?\d+(?:\.\d+)+")

# Appended to an assumed_version fallback result so it's never mistaken
# for the same quality of information as the rest of this chain (both of
# which are verified: a self-reported OCI label, or the pinned tag
# itself) — see UNVERIFIED_DETECTED_VERSION_SPEC.md.
UNVERIFIED_SUFFIX = " (unverified)"


class NoReleaseMatch:
    """Sentinel, distinct from None: a live digest search ran against real
    ranked candidate tags and genuinely found no match — as opposed to
    None, meaning no search was even attempted (no digest to search
    with, or no candidates to search among). Confirmed real case: GitHub's
    own compare view shows onstar2mqtt's running build 20 commits ahead
    of its newest tagged release (v2.10.1) — a real search against real
    candidates, correctly finding nothing, not an absence of information.
    See tag_filter.py's classify_digest_search_result() and
    tag_walk_coordinator.py's _search_ranked_for_digest().

    Deliberately falsy (__bool__ below), not a plain string living
    directly in ServiceTagWalkStatus/detect_version()'s data — every
    *existing* truthy check elsewhere (e.g. pull_jobs.py's
    `if tag_walk_status.pull_target_version:` guard, used to pick an
    assumed_version to persist) keeps treating this exactly like None —
    the same safe default — without needing to be audited or touched.
    Only the specific call sites that need to *render* this distinctly
    (ServicePullTargetVersionSensor, detect_version() below) check for it
    explicitly, by identity (`is NO_RELEASE_MATCH`), since identity is the
    only thing distinguishing it from a falsy None in the first place.
    """

    def __repr__(self) -> str:
        return "NO_RELEASE_MATCH"

    def __bool__(self) -> bool:
        return False


NO_RELEASE_MATCH = NoReleaseMatch()

# The literal displayed value for NO_RELEASE_MATCH, shared by
# detect_version() below and sensor.py's ServicePullTargetVersionSensor,
# so both sensors ("Current version"/detected_version and "Pull target
# version"/pull_target_version) that can carry this sentinel render it
# identically — see REGISTRY_TAG_WALK_SPEC.md's unreleased-build
# amendment for why these two are treated symmetrically.
UNRELEASED_VERSION_LABEL = "unreleased"


def is_version_tag(tag: str) -> bool:
    """A tag counts as a real version if it's not a known floating/named
    tag and starts with a digit (optionally preceded by a 'v')."""
    if not tag:
        return False
    if tag.lower() in NON_VERSION_TAGS:
        return False
    return bool(_LOOKS_LIKE_VERSION_RE.match(tag))


def has_version_structure(value: str) -> bool:
    """Stricter shape check than is_version_tag(): requires at least two
    dot-separated numeric components, not just a leading digit. See
    _VERSION_STRUCTURE_RE above for why this exists and where it's used
    (this module's own OCI-label validation, and tag_filter.py's registry
    tag-list filtering)."""
    return bool(_VERSION_STRUCTURE_RE.match(value))


def normalize_version(tag: str) -> str:
    """Strip a leading 'v' and any package-name@ prefix (e.g. 'n8n@2.9.4'
    -> '2.9.4'). Only applied to tag-derived values — a label value is
    used verbatim, since labels are normally already a clean version
    string."""
    if "@" in tag:
        tag = tag.rsplit("@", 1)[-1]
    if _LEADING_V_RE.match(tag):
        tag = tag[1:]
    return tag


def detect_version(
    image_ref: str,
    image_labels: dict[str, str],
    container_labels: dict[str, str] | None,
    *,
    stack_name: str = "",
    service_name: str = "",
    verified_current_version: str | NoReleaseMatch | None = None,
    assumed_version: str | None = None,
) -> str | None:
    """Fallback chain, checked in order:

    1. The OCI version label (image first, then the container's own
       labels as a fallback for a user-supplied compose `labels:`
       override — same resolution order as the GitHub source-label
       lookup, see oci_labels.py).
    2. The image tag itself, if it looks like a real version.
    3. `verified_current_version` (caller-supplied; this function still
       has no I/O of its own) — a live match found by cross-referencing
       the registry's own tags against the running image's digest (see
       TagWalkCoordinator.ServiceTagWalkStatus.verified_current_version
       and UNVERIFIED_DETECTED_VERSION_SPEC.md's verified-current-version
       amendment). This is what makes a floating tag (stable, latest,
       edge, ...) resolve to a real version at all when neither the
       label nor the tag itself has one to parse — returned verbatim, no
       UNVERIFIED_SUFFIX, since a digest match is a proven fact, not an
       assumption, exactly like a real pin would be.
    4. `NO_RELEASE_MATCH` — a distinct case of `verified_current_version`,
       checked immediately after step 3 (which only handles a real,
       truthy match): the same live digest search ran, but genuinely
       found no matching released tag at all, meaning the running build
       is ahead of any tagged release. Returns UNRELEASED_VERSION_LABEL
       ("unreleased"), checked *before* assumed_version — a proven live
       negative outranks an older, unverified guess, rather than being
       silently overridden by it.
    5. Strictly last resort, and only if every check above returns
       nothing — `assumed_version`, a persisted snapshot of
       `latest_registry_tag` from the moment of the most recent
       successful pull (see pull_jobs.py/storage.py). Deliberately
       weaker than steps 3/4 and checked after them: an assumption
       (pulling a floating tag usually, by convention, lands on the same
       build as the newest published tag, but this was never proven the
       way a digest match is) rather than a verified fact, and marked
       with UNVERIFIED_SUFFIX in the returned value so it's never
       mistaken for one.

    Returns None (never raises) if nothing at all is available — that's
    the expected outcome for some images on a floating tag with no
    version label, no matching registry tag, and no prior pull, not an
    error.
    """
    label_value, origin = resolve_label(image_labels, container_labels, OCI_VERSION_LABEL)
    if label_value and has_version_structure(label_value):
        _LOGGER.debug(
            "detect_version(%s/%s): using '%s' label from the %s: %r",
            stack_name,
            service_name,
            OCI_VERSION_LABEL,
            origin,
            label_value,
        )
        return label_value
    if label_value:
        # Some images' build pipelines populate this label with something
        # other than a real version — a known floating-tag word like
        # "latest" (a docker/metadata-action-style CI template passing the
        # tag through unconditionally), or an arbitrary non-version
        # placeholder like "weekly" (confirmed real case: onstar2mqtt sets
        # this label to its release-cadence name, not a version). Neither
        # is trustworthy just because it came from a label, so this
        # structural check (has_version_structure(), not a word-list
        # lookup — closes the whole category rather than one word at a
        # time) rejects both the same way. Fall through to the tag check.
        _LOGGER.debug(
            "detect_version(%s/%s): '%s' label from the %s is %r — doesn't look like a real "
            "version (no dot-separated digit structure) — ignoring it and checking the image "
            "tag instead",
            stack_name,
            service_name,
            OCI_VERSION_LABEL,
            origin,
            label_value,
        )

    image_tag = extract_tag(image_ref)
    if is_version_tag(image_tag):
        detected = normalize_version(image_tag)
        _LOGGER.debug(
            "detect_version(%s/%s): using image tag '%s' (normalized to %r)",
            stack_name,
            service_name,
            image_tag,
            detected,
        )
        return detected

    if verified_current_version:
        _LOGGER.debug(
            "detect_version(%s/%s): no '%s' label and tag '%s' doesn't look like a version — "
            "using verified_current_version %r (a live registry-tag/digest cross-reference, "
            "not an assumption)",
            stack_name,
            service_name,
            OCI_VERSION_LABEL,
            image_tag,
            verified_current_version,
        )
        return verified_current_version

    if verified_current_version is NO_RELEASE_MATCH:
        _LOGGER.debug(
            "detect_version(%s/%s): no '%s' label and tag '%s' doesn't look like a version — "
            "a live digest search ran and found no matching released tag at all (running build "
            "is ahead of any tagged release) — showing %r rather than falling through to a "
            "weaker, unverified assumed_version guess",
            stack_name,
            service_name,
            OCI_VERSION_LABEL,
            image_tag,
            UNRELEASED_VERSION_LABEL,
        )
        return UNRELEASED_VERSION_LABEL

    if assumed_version:
        _LOGGER.debug(
            "detect_version(%s/%s): no '%s' label and tag '%s' doesn't look like a version — "
            "falling back to assumed_version %r from the last successful pull, marked "
            "unverified",
            stack_name,
            service_name,
            OCI_VERSION_LABEL,
            image_tag,
            assumed_version,
        )
        return f"{assumed_version}{UNVERIFIED_SUFFIX}"

    _LOGGER.debug(
        "detect_version(%s/%s): no '%s' label, tag '%s' doesn't look like a version, no "
        "verified_current_version match, and no assumed_version from a prior pull either — "
        "nothing to show (expected/normal, not an error)",
        stack_name,
        service_name,
        OCI_VERSION_LABEL,
        image_tag,
    )
    return None
