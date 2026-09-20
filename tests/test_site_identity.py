from ha_docker_compose.site_identity import (
    other_site_slugs,
    resolve_unique_site_slug,
    slugify_site_name,
)


def test_slugify_site_name_lowercases_and_replaces_invalid_chars() -> None:
    assert slugify_site_name("My NAS!") == "my_nas"


def test_slugify_site_name_collapses_runs_and_strips_edges() -> None:
    assert slugify_site_name("  --Raspberry Pi--  ") == "raspberry_pi"


def test_slugify_site_name_falls_back_for_empty_result() -> None:
    assert slugify_site_name("") == "site"
    assert slugify_site_name("---") == "site"


def test_resolve_unique_site_slug_returns_desired_when_free() -> None:
    assert resolve_unique_site_slug("nas", {"pi"}) == "nas"


def test_resolve_unique_site_slug_auto_suffixes_on_collision() -> None:
    # Two entries with identical leaf folder names ("docker") both derive
    # the same default slug — the second one landing here must not clobber
    # the first's.
    assert resolve_unique_site_slug("docker", {"docker"}) == "docker_2"


def test_resolve_unique_site_slug_skips_past_multiple_taken_suffixes() -> None:
    assert resolve_unique_site_slug("docker", {"docker", "docker_2", "docker_3"}) == "docker_4"


def test_other_site_slugs_excludes_the_given_entry_id() -> None:
    entries = [("entry-a", "nas"), ("entry-b", "pi")]
    assert other_site_slugs(entries, exclude_entry_id="entry-a") == {"pi"}


def test_other_site_slugs_ignores_entries_with_no_resolved_slug_yet() -> None:
    entries = [("entry-a", "nas"), ("entry-b", None)]
    assert other_site_slugs(entries, exclude_entry_id=None) == {"nas"}
