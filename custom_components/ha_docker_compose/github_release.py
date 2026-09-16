"""Best-effort "latest GitHub release" lookup.

Explicitly NOT linked to update-available or to what's currently running
(see PROJECT_SPEC.md's Update Tracking section) — this is separate,
informational, best-effort data with nothing implying your current tag
would ever resolve to the release reported here.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

_LOGGER = logging.getLogger(__name__)

OCI_SOURCE_LABEL = "org.opencontainers.image.source"

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?/?$"
)


@dataclass
class GitHubRelease:
    tag_name: str
    published_at: str | None
    html_url: str | None
    # The maintainer-written changelog/notes for this release, verbatim
    # markdown from the API response — None if GitHub has nothing for this
    # field (some releases are tagged with no written notes at all).
    body: str | None = None


def parse_github_repo(source_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from an org.opencontainers.image.source value.

    Only matches a bare github.com repo URL, not e.g. a subpath into a
    monorepo — this label isn't universal to begin with (spec: "absent for
    some images, in which case this feature simply has nothing to show"),
    so a non-match is expected/normal, not an error.
    """
    match = _GITHUB_URL_RE.match(source_url.strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


async def fetch_latest_release(
    session: aiohttp.ClientSession, owner: str, repo: str
) -> GitHubRelease | None:
    """Best-effort: returns None on 404 (no releases), rate limiting, or any
    other failure — this feature simply has nothing to show in that case."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                _LOGGER.debug(
                    "GitHub releases/latest for %s/%s returned HTTP %s (rate limited, no "
                    "releases, or repo/name mismatch): %s",
                    owner,
                    repo,
                    resp.status,
                    body[:500],
                )
                return None
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("GitHub release lookup failed for %s/%s: %s", owner, repo, err)
        return None

    tag_name = data.get("tag_name")
    if not tag_name:
        _LOGGER.debug(
            "GitHub releases/latest for %s/%s returned 200 but no tag_name field", owner, repo
        )
        return None

    return GitHubRelease(
        tag_name=tag_name,
        published_at=data.get("published_at"),
        html_url=data.get("html_url"),
        # Zero additional API cost — the field is already present on this
        # same /releases/latest response, just previously discarded. Kept
        # as-is (raw markdown), no length limit, no rendering.
        body=data.get("body"),
    )
