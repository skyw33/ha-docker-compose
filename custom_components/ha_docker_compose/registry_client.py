"""Native Docker Registry HTTP API v2 client for remote digest lookups.

Replaces the skopeo subprocess dependency (see UPDATE_CHECK_NATIVE_SPEC.md)
with pure aiohttp calls, fitting directly into the existing async
coordinator pattern — no host binary, no `docker run`, no subprocess for
this feature at all.

Scope: any registry that speaks the common anonymous-bearer-token-auth v2
flow (Docker Hub and ghcr.io are the two wired up for v1 — see REGISTRIES
below). Adding another such registry later is one table entry, not a new
code path — see RegistryClient. Registries needing different auth (e.g.
basic auth for some private/self-hosted setups) still need bespoke handling
whenever added; that's RegistryUnsupportedError's job, not a gap to "fix"
by force-fitting them into this client.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

from .image_ref import parse_image_ref

_LOGGER = logging.getLogger(__name__)

MANIFEST_ACCEPT_HEADER = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

# Docker Registry HTTP API v2's tags/list endpoint pages large tag lists
# via a standard-ish `Link: <url>; rel="next"` response header (same idea
# as GitHub's pagination). Some registries (e.g. Docker Hub) return
# everything in one page for most repositories; others (ghcr.io included,
# for a repo with enough tags) paginate. This hard cap exists only to
# bound worst-case traffic against a misbehaving registry that never stops
# handing back a `next` link — not expected to ever be hit in practice.
TAG_LIST_MAX_PAGES = 50

# Requested explicitly via `?n=` on every tags/list call — confirmed by
# direct testing against ghcr.io that omitting this entirely (as this
# client did before) gets ghcr.io's own default of 100 tags/page, while
# 1000 is honored exactly and is also the observed ceiling (requesting
# more, e.g. 5000 or 10000, still returns only 1000 — ghcr.io silently
# clamps rather than erroring). At the previous no-`n=` default, a repo
# with enough non-version noise tags (e.g. per-commit CI builds) could
# exhaust the entire TAG_LIST_MAX_PAGES budget (50 x 100 = 5,000 tags)
# without ever reaching a real, wanted version tag that happened to sit
# later in the registry's own ordering — confirmed real case:
# ghcr.io/blakeblackshear/frigate, 15,000+ tags, dominated by per-commit
# dev tags, where the actual current release tag never appeared within
# the old 5,000-tag budget at all. Also tested against Docker Hub: with
# no `n=` at all, Hub returns everything unpaginated for a repo under its
# own threshold (confirmed: 1339 tags, one response, no Link header) —
# sending an explicit `n=1000` there instead makes Hub paginate a repo
# that size into two requests rather than one. A minor, accepted
# trade-off for using one shared value/implementation across both
# registries rather than special-casing per registry — see this module's
# own docstring on why that's the design here.
TAG_LIST_PAGE_SIZE = 1000

# Purely diagnostic (see the info-log call site in _get_tags): direct
# testing against ghcr.io/esphome/esphome (2,890+ tags) consistently
# showed a per-page cap around 100-1000 tags with a Link header attached
# whenever more results remained — never everything in one unpaginated
# page for a repository this size. A final page (no Link header) larger
# than this is unexpected enough to log for visibility.
UNPAGINATED_LARGE_PAGE_THRESHOLD = 500

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class RegistryError(Exception):
    """Transient failure (network/timeout/bad response) fetching a digest.

    Callers should keep the last-known digest and retry next poll — see
    UPDATE_CHECK_NATIVE_SPEC.md's error-handling rules.
    """


class RegistryAuthError(Exception):
    """401/403 — most likely a private image with no credentials configured
    (anonymous token auth only; no credential-store UI in scope for v1)."""


class RegistryUnsupportedError(Exception):
    """The image's registry host isn't in REGISTRIES."""


@dataclass(frozen=True)
class RegistryConfig:
    auth_url: str
    registry_url: str
    service: str


REGISTRIES: dict[str, RegistryConfig] = {
    "docker.io": RegistryConfig(
        auth_url="https://auth.docker.io/token",
        registry_url="https://registry-1.docker.io",
        service="registry.docker.io",
    ),
    "ghcr.io": RegistryConfig(
        auth_url="https://ghcr.io/token",
        registry_url="https://ghcr.io",
        service="ghcr.io",
    ),
}


class RegistryClient:
    """Generic anonymous-token-auth Docker Registry HTTP API v2 client.

    Docker Hub and ghcr.io are two instances of this one flow (token fetch,
    then a manifest fetch with that token), not two separate
    implementations — see REGISTRIES for what varies between them.
    """

    def __init__(self, session: aiohttp.ClientSession, timeout: float = 10.0) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def get_manifest_digest(self, image_ref: str) -> str:
        """Return the remote manifest digest for image_ref (e.g. 'nginx:latest').

        Raises RegistryUnsupportedError / RegistryAuthError / RegistryError
        so callers can distinguish "permanently can't check this" from
        "transient failure, keep the last known value".
        """
        parsed = parse_image_ref(image_ref)
        config = REGISTRIES.get(parsed.registry)
        if config is None:
            raise RegistryUnsupportedError(f"Unsupported registry: {parsed.registry}")

        token = await self._get_token(config, parsed.repository)
        return await self._get_digest(config, parsed.repository, parsed.tag, token)

    async def list_tags(self, image_ref: str, *, log_context: str) -> list[str]:
        """Return every tag published on image_ref's repository (the tag
        portion of image_ref itself is ignored — this lists the whole
        repository), handling pagination if the registry returns one.

        Same RegistryUnsupportedError/RegistryAuthError/RegistryError
        distinction as get_manifest_digest, for the same reason: callers
        need to tell "permanently can't check this" apart from "transient
        failure, keep the last known value".

        log_context: a caller-supplied label (e.g. "stack X, service Y")
        included in this method's own diagnostic logs — required, not
        optional, since there's exactly one caller today
        (tag_walk_coordinator.py) and it always has this on hand. This
        client stays registry-generic otherwise (no notion of "stack" or
        "service" anywhere else in it) — it's purely for making a
        pagination-cap warning or a page-by-page debug trace identifiable
        without cross-referencing the repository name back to a stack.
        """
        parsed = parse_image_ref(image_ref)
        config = REGISTRIES.get(parsed.registry)
        if config is None:
            raise RegistryUnsupportedError(f"Unsupported registry: {parsed.registry}")

        token = await self._get_token(config, parsed.repository)
        return await self._get_tags(config, parsed.repository, token, log_context)

    async def _get_token(self, config: RegistryConfig, repository: str) -> str:
        params = {"service": config.service, "scope": f"repository:{repository}:pull"}
        try:
            async with self._session.get(
                config.auth_url, params=params, timeout=self._timeout
            ) as resp:
                if resp.status in (401, 403):
                    raise RegistryAuthError(f"Auth token request denied ({resp.status})")
                if resp.status != 200:
                    raise RegistryError(f"Token request failed: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise RegistryError(f"Token request failed: {err}") from err
        except TimeoutError as err:
            raise RegistryError("Token request timed out") from err

        token = data.get("token") or data.get("access_token")
        if not token:
            raise RegistryError("Token response had no token/access_token field")
        return token

    async def _get_digest(self, config: RegistryConfig, repository: str, tag: str, token: str) -> str:
        url = f"{config.registry_url}/v2/{repository}/manifests/{tag}"
        headers = {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT_HEADER}
        try:
            async with self._session.get(url, headers=headers, timeout=self._timeout) as resp:
                if resp.status in (401, 403):
                    raise RegistryAuthError(f"Manifest request denied ({resp.status})")
                if resp.status != 200:
                    raise RegistryError(f"Manifest request failed: HTTP {resp.status}")
                digest = resp.headers.get("Docker-Content-Digest")
        except aiohttp.ClientError as err:
            raise RegistryError(f"Manifest request failed: {err}") from err
        except TimeoutError as err:
            raise RegistryError("Manifest request timed out") from err

        if not digest:
            raise RegistryError("Manifest response had no Docker-Content-Digest header")
        return digest

    async def _get_tags(
        self, config: RegistryConfig, repository: str, token: str, log_context: str
    ) -> list[str]:
        # n= only needs setting on this first request — the registry's own
        # Link header for every subsequent page already carries it forward
        # (confirmed by direct testing), so _parse_next_link()'s URLs never
        # need it re-added.
        url = f"{config.registry_url}/v2/{repository}/tags/list?n={TAG_LIST_PAGE_SIZE}"
        headers = {"Authorization": f"Bearer {token}"}
        tags: list[str] = []

        for page_num in range(1, TAG_LIST_MAX_PAGES + 1):
            try:
                async with self._session.get(url, headers=headers, timeout=self._timeout) as resp:
                    if resp.status in (401, 403):
                        raise RegistryAuthError(f"Tag list request denied ({resp.status})")
                    if resp.status != 200:
                        raise RegistryError(f"Tag list request failed: HTTP {resp.status}")
                    data = await resp.json(content_type=None)
                    link_header = resp.headers.get("Link")
                    next_url = _parse_next_link(link_header, config.registry_url)
            except aiohttp.ClientError as err:
                raise RegistryError(f"Tag list request failed: {err}") from err
            except TimeoutError as err:
                raise RegistryError("Tag list request timed out") from err

            page_tags = data.get("tags") or []
            tags.extend(page_tags)
            # Verbose by design — this is the ground truth for "what did the
            # registry actually hand back", the first place to look when a
            # tag shows up (or is missing) downstream and it's unclear
            # whether the registry sent it or a later stage introduced it.
            _LOGGER.debug(
                "list_tags(%s, %s): page %d fetched %d tags (running total %d), Link "
                "header=%r, next_url=%r",
                repository,
                log_context,
                page_num,
                len(page_tags),
                len(tags),
                link_header,
                next_url,
            )
            if next_url is None:
                if len(page_tags) > UNPAGINATED_LARGE_PAGE_THRESHOLD:
                    # Every page observed from ghcr.io/Docker Hub during
                    # development topped out at ~100-1000 tags with a Link
                    # header attached whenever more remained (confirmed by
                    # direct testing, including an explicit large `n=`
                    # request, which was still capped and still paginated).
                    # A single page this large with no Link header at all
                    # is unexpected enough to be worth a visible trace if
                    # it recurs — not necessarily wrong (a compliant
                    # registry is allowed to return everything at once),
                    # but real-world evidence hasn't shown this registry
                    # actually doing that, so it's flagged rather than
                    # silently trusted.
                    _LOGGER.info(
                        "list_tags(%s, %s): page %d returned %d tags with no Link header for "
                        "continuation — unusually large for a single unpaginated page compared "
                        "to this registry's normal per-page behavior; noting in case pagination "
                        "behavior is inconsistent for this repository",
                        repository,
                        log_context,
                        page_num,
                        len(page_tags),
                    )
                break
            url = next_url
        else:
            _LOGGER.warning(
                "Tag list for %s (%s) hit the %d-page pagination cap (%d tags fetched) — "
                "result may be incomplete",
                repository,
                log_context,
                TAG_LIST_MAX_PAGES,
                len(tags),
            )

        return tags


def _parse_next_link(link_header: str | None, registry_url: str) -> str | None:
    """Extract the `next` URL from a Link header, resolving a
    registry-relative path (e.g. `/v2/name/tags/list?last=...`) against
    registry_url the same way a browser resolves a relative href."""
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    if not match:
        return None
    next_ref = match.group(1)
    if next_ref.startswith("http://") or next_ref.startswith("https://"):
        return next_ref
    return f"{registry_url.rstrip('/')}/{next_ref.lstrip('/')}"
