import pytest

from ha_docker_compose.github_release import fetch_latest_release, parse_github_repo


def test_parse_plain_url() -> None:
    assert parse_github_repo("https://github.com/owner/repo") == ("owner", "repo")


def test_parse_without_scheme() -> None:
    assert parse_github_repo("github.com/owner/repo") == ("owner", "repo")


def test_parse_with_git_suffix() -> None:
    assert parse_github_repo("https://github.com/owner/repo.git") == ("owner", "repo")


def test_parse_with_trailing_slash() -> None:
    assert parse_github_repo("https://github.com/owner/repo/") == ("owner", "repo")


def test_parse_non_github_url_returns_none() -> None:
    assert parse_github_repo("https://gitlab.com/owner/repo") is None


def test_parse_github_subpath_returns_none() -> None:
    assert parse_github_repo("https://github.com/owner/repo/tree/main/sub") is None


class _FakeResponse:
    def __init__(self, status=200, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data or {}
        self._text_data = text_data
        self.calls = 0

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.get_calls: list[dict] = []

    def get(self, url, timeout=None):
        self.get_calls.append({"url": url})
        return self._response


@pytest.mark.asyncio
async def test_fetch_latest_release_keeps_body_and_html_url() -> None:
    response = _FakeResponse(
        status=200,
        json_data={
            "tag_name": "v0.18.0",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/releases/tag/v0.18.0",
            "body": "## What's Changed\n- Fixed a bug\n",
        },
    )
    session = _FakeSession(response)

    release = await fetch_latest_release(session, "owner", "repo")

    assert release is not None
    assert release.tag_name == "v0.18.0"
    assert release.html_url == "https://github.com/owner/repo/releases/tag/v0.18.0"
    assert release.body == "## What's Changed\n- Fixed a bug\n"
    assert session.get_calls[0]["url"] == "https://api.github.com/repos/owner/repo/releases/latest"


@pytest.mark.asyncio
async def test_fetch_latest_release_null_body_does_not_error() -> None:
    """Real-world case: some releases are tagged with no written notes at
    all, so GitHub's API returns body: null."""
    response = _FakeResponse(
        status=200,
        json_data={
            "tag_name": "v1.0.0",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/releases/tag/v1.0.0",
            "body": None,
        },
    )
    session = _FakeSession(response)

    release = await fetch_latest_release(session, "owner", "repo")

    assert release is not None
    assert release.body is None


@pytest.mark.asyncio
async def test_fetch_latest_release_missing_body_field_does_not_error() -> None:
    response = _FakeResponse(
        status=200,
        json_data={
            "tag_name": "v1.0.0",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/releases/tag/v1.0.0",
            # no "body" key at all
        },
    )
    session = _FakeSession(response)

    release = await fetch_latest_release(session, "owner", "repo")

    assert release is not None
    assert release.body is None


@pytest.mark.asyncio
async def test_fetch_latest_release_makes_exactly_one_call() -> None:
    response = _FakeResponse(
        status=200,
        json_data={"tag_name": "v1.0.0", "published_at": None, "html_url": None, "body": "notes"},
    )
    session = _FakeSession(response)

    await fetch_latest_release(session, "owner", "repo")

    assert len(session.get_calls) == 1


@pytest.mark.asyncio
async def test_fetch_latest_release_non_200_returns_none() -> None:
    response = _FakeResponse(status=404, text_data="Not Found")
    session = _FakeSession(response)

    release = await fetch_latest_release(session, "owner", "repo")

    assert release is None
