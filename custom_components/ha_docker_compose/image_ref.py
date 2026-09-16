"""Small helpers for parsing Docker image references (name[:tag][@digest])."""
from __future__ import annotations

from dataclasses import dataclass


def repo_name(image_ref: str) -> str:
    """Return the repository name with any :tag or @digest stripped."""
    if "@" in image_ref:
        return image_ref.split("@", 1)[0]
    name, sep, candidate = image_ref.rpartition(":")
    if sep and "/" not in candidate:
        return name
    return image_ref


def tag(image_ref: str, default: str = "latest") -> str:
    """Return the tag portion of an image reference, defaulting to 'latest'
    (Compose's own default when a tag is omitted). A digest-pinned
    reference (`image@sha256:...`) has no tag concept, so it also falls
    back to the default."""
    if "@" in image_ref:
        return default
    name, sep, candidate = image_ref.rpartition(":")
    if sep and "/" not in candidate:
        return candidate
    return default


@dataclass(frozen=True)
class ParsedImageRef:
    """Registry-API-ready form of an image reference."""

    registry: str
    repository: str
    tag: str


def _looks_like_host(segment: str) -> bool:
    return "." in segment or ":" in segment or segment == "localhost"


def parse_image_ref(image_ref: str) -> ParsedImageRef:
    """Normalize an image reference into (registry, repository, tag) for
    registry-API use.

    Distinct from repo_name()/tag() above, which describe a reference the
    way the local Docker daemon stores it (no implicit `library/` prefix,
    since that's not how a locally pulled image's RepoDigests are keyed) —
    this adds that prefix, which the Docker Hub v2 API requires for
    official images.
    """
    ref = image_ref.split("@", 1)[0]  # digest-pinned refs: tag defaults below, same as tag()

    first_slash = ref.find("/")
    if first_slash != -1 and _looks_like_host(ref[:first_slash]):
        registry = ref[:first_slash]
        remainder = ref[first_slash + 1 :]
    else:
        registry = "docker.io"
        remainder = ref

    name, sep, candidate = remainder.rpartition(":")
    if sep and "/" not in candidate:
        repository, tag_value = name, candidate
    else:
        repository, tag_value = remainder, "latest"

    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"

    return ParsedImageRef(registry=registry, repository=repository, tag=tag_value)
