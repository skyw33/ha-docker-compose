import pytest

from ha_docker_compose.registry_client import (
    REGISTRIES,
    TAG_LIST_MAX_PAGES,
    TAG_LIST_PAGE_SIZE,
    RegistryAuthError,
    RegistryClient,
    RegistryError,
    RegistryUnsupportedError,
)


class FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None):
        self.status = status
        self._json_data = json_data or {}
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession: .get() returns a canned
    response (an async context manager) synchronously, matching aiohttp's
    own real behavior of returning a context manager without awaiting."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_manifest_digest_success() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=200, headers={"Docker-Content-Digest": "sha256:deadbeef"}),
        ]
    )
    client = RegistryClient(session)

    digest = await client.get_manifest_digest("nginx:latest")

    assert digest == "sha256:deadbeef"
    assert session.calls[0]["url"] == REGISTRIES["docker.io"].auth_url
    assert session.calls[0]["params"]["scope"] == "repository:library/nginx:pull"
    assert (
        session.calls[1]["url"]
        == f"{REGISTRIES['docker.io'].registry_url}/v2/library/nginx/manifests/latest"
    )
    assert session.calls[1]["headers"]["Authorization"] == "Bearer abc123"


@pytest.mark.asyncio
async def test_ghcr_uses_ghcr_endpoints() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "tok"}),
            FakeResponse(status=200, headers={"Docker-Content-Digest": "sha256:cafe"}),
        ]
    )
    client = RegistryClient(session)

    digest = await client.get_manifest_digest("ghcr.io/owner/repo:tag")

    assert digest == "sha256:cafe"
    assert session.calls[0]["url"] == REGISTRIES["ghcr.io"].auth_url
    assert session.calls[1]["url"] == "https://ghcr.io/v2/owner/repo/manifests/tag"


@pytest.mark.asyncio
async def test_unsupported_registry_raises_without_network_call() -> None:
    session = FakeSession([])
    client = RegistryClient(session)

    with pytest.raises(RegistryUnsupportedError):
        await client.get_manifest_digest("quay.io/owner/repo:tag")

    assert session.calls == []


@pytest.mark.asyncio
async def test_auth_denied_on_token_fetch() -> None:
    session = FakeSession([FakeResponse(status=401)])
    client = RegistryClient(session)

    with pytest.raises(RegistryAuthError):
        await client.get_manifest_digest("ghcr.io/owner/private:latest")


@pytest.mark.asyncio
async def test_auth_denied_on_manifest_fetch() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "tok"}),
            FakeResponse(status=403),
        ]
    )
    client = RegistryClient(session)

    with pytest.raises(RegistryAuthError):
        await client.get_manifest_digest("ghcr.io/owner/private:latest")


@pytest.mark.asyncio
async def test_manifest_fetch_failure_raises_registry_error() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=500),
        ]
    )
    client = RegistryClient(session)

    with pytest.raises(RegistryError):
        await client.get_manifest_digest("nginx:latest")


@pytest.mark.asyncio
async def test_missing_digest_header_raises_registry_error() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=200, headers={}),
        ]
    )
    client = RegistryClient(session)

    with pytest.raises(RegistryError):
        await client.get_manifest_digest("nginx:latest")


@pytest.mark.asyncio
async def test_list_tags_single_page() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=200, json_data={"tags": ["1.0.0", "2.0.0", "latest"]}),
        ]
    )
    client = RegistryClient(session)

    tags = await client.list_tags("nginx:latest", log_context="test")

    assert tags == ["1.0.0", "2.0.0", "latest"]
    assert (
        session.calls[1]["url"]
        == f"{REGISTRIES['docker.io'].registry_url}/v2/library/nginx/tags/list?n={TAG_LIST_PAGE_SIZE}"
    )
    assert session.calls[1]["headers"]["Authorization"] == "Bearer abc123"


@pytest.mark.asyncio
async def test_list_tags_follows_link_header_pagination() -> None:
    registry_url = REGISTRIES["ghcr.io"].registry_url
    next_url = f"{registry_url}/v2/owner/repo/tags/list?last=b&n=2"
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "tok"}),
            FakeResponse(
                status=200,
                json_data={"tags": ["a", "b"]},
                headers={"Link": f'<{next_url}>; rel="next"'},
            ),
            FakeResponse(status=200, json_data={"tags": ["c"]}),
        ]
    )
    client = RegistryClient(session)

    tags = await client.list_tags("ghcr.io/owner/repo:latest", log_context="test")

    assert tags == ["a", "b", "c"]
    assert session.calls[2]["url"] == next_url


@pytest.mark.asyncio
async def test_list_tags_resolves_relative_link_header() -> None:
    registry_url = REGISTRIES["docker.io"].registry_url
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(
                status=200,
                json_data={"tags": ["1.0.0"]},
                headers={"Link": '</v2/library/nginx/tags/list?last=1.0.0>; rel="next"'},
            ),
            FakeResponse(status=200, json_data={"tags": ["2.0.0"]}),
        ]
    )
    client = RegistryClient(session)

    tags = await client.list_tags("nginx:latest", log_context="test")

    assert tags == ["1.0.0", "2.0.0"]
    assert session.calls[2]["url"] == f"{registry_url}/v2/library/nginx/tags/list?last=1.0.0"


@pytest.mark.asyncio
async def test_list_tags_unsupported_registry_raises_without_network_call() -> None:
    session = FakeSession([])
    client = RegistryClient(session)

    with pytest.raises(RegistryUnsupportedError):
        await client.list_tags("quay.io/owner/repo:tag", log_context="test")

    assert session.calls == []


@pytest.mark.asyncio
async def test_list_tags_auth_denied() -> None:
    session = FakeSession([FakeResponse(status=401)])
    client = RegistryClient(session)

    with pytest.raises(RegistryAuthError):
        await client.list_tags("ghcr.io/owner/private:latest", log_context="test")


@pytest.mark.asyncio
async def test_list_tags_large_unpaginated_page_is_still_returned_correctly(caplog) -> None:
    """A single page over the diagnostic threshold with no Link header is
    unusual (see UNPAGINATED_LARGE_PAGE_THRESHOLD) but not an error — the
    tags are still returned in full; only a visibility log fires."""
    big_page = [f"1.0.{i}" for i in range(501)]
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=200, json_data={"tags": big_page}),
        ]
    )
    client = RegistryClient(session)

    with caplog.at_level("INFO"):
        tags = await client.list_tags("nginx:latest", log_context="test")

    assert tags == big_page
    assert any("unusually large" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_list_tags_request_failure_raises_registry_error() -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=500),
        ]
    )
    client = RegistryClient(session)

    with pytest.raises(RegistryError):
        await client.list_tags("nginx:latest", log_context="test")


@pytest.mark.asyncio
async def test_list_tags_first_request_includes_explicit_page_size() -> None:
    """Confirmed by direct testing against ghcr.io: omitting `n=` entirely
    gets its own default of 100 tags/page, while an explicit `n=1000` is
    honored (and is also the observed ceiling) — see TAG_LIST_PAGE_SIZE's
    own docstring. A repo dominated by non-version tags (confirmed real
    case: ghcr.io/blakeblackshear/frigate, 15,000+ tags) could exhaust the
    old 5,000-tag budget (50 pages x the old 100/page default) without
    ever reaching a real version tag sitting later in the registry's own
    order."""
    session = FakeSession(
        [
            FakeResponse(status=200, json_data={"token": "abc123"}),
            FakeResponse(status=200, json_data={"tags": ["1.0.0"]}),
        ]
    )
    client = RegistryClient(session)

    await client.list_tags("nginx:latest", log_context="test")

    assert session.calls[1]["url"].endswith(f"?n={TAG_LIST_PAGE_SIZE}")
    assert TAG_LIST_PAGE_SIZE >= 1000  # the verified ghcr.io ceiling


@pytest.mark.asyncio
async def test_list_tags_pagination_cap_warning_names_repo_and_context(caplog) -> None:
    """The pagination-cap warning must identify which repo AND which
    stack/service it was for — with only the repo name (as before), the
    log line alone can't tell you where to look in your own compose
    files."""
    registry_url = REGISTRIES["ghcr.io"].registry_url
    responses = [FakeResponse(status=200, json_data={"token": "tok"})]
    for page in range(TAG_LIST_MAX_PAGES):
        next_url = f"{registry_url}/v2/owner/repo/tags/list?last=tag{page}"
        responses.append(
            FakeResponse(
                status=200,
                json_data={"tags": [f"tag{page}"]},
                headers={"Link": f'<{next_url}>; rel="next"'},
            )
        )
    session = FakeSession(responses)
    client = RegistryClient(session)

    with caplog.at_level("WARNING"):
        tags = await client.list_tags(
            "ghcr.io/owner/repo:latest", log_context="stack media, service frigate"
        )

    assert len(tags) == TAG_LIST_MAX_PAGES
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "owner/repo" in warnings[0]
    assert "stack media, service frigate" in warnings[0]
    assert "pagination cap" in warnings[0]
