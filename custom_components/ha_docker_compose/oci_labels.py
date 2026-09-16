"""Shared "check image labels first, container labels as fallback" logic.

Compose applies a service's `labels:` block to the container it creates,
not to the image — a label a user manually adds that way only shows up via
container inspection. This resolution order was first built for the GitHub
release source-label lookup (github_coordinator.py) and is reused as-is by
local version detection (version_detect.py) rather than being
reimplemented per feature.
"""
from __future__ import annotations


def resolve_label(
    image_labels: dict[str, str], container_labels: dict[str, str] | None, label_key: str
) -> tuple[str | None, str | None]:
    """Return (value, source), source being "image", "container", or None.

    The image is authoritative when it has the label at all; the
    container's labels (compose `labels:` override) are only consulted if
    the image itself doesn't set it — and only if a container currently
    exists to check (container_labels may be None, e.g. a stopped stack).
    """
    value = image_labels.get(label_key)
    if value:
        return value, "image"

    if container_labels:
        value = container_labels.get(label_key)
        if value:
            return value, "container"

    return None, None
