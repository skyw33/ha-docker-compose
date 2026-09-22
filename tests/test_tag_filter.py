from ha_docker_compose.tag_filter import (
    filter_tags,
    is_real_version_tag,
    rank_final_versions,
    select_newest_version_tag,
)


def test_is_real_version_tag_accepts_bare_digit() -> None:
    assert is_real_version_tag("2.9.4") is True


def test_is_real_version_tag_accepts_v_prefixed() -> None:
    assert is_real_version_tag("v4.2.0") is True


def test_is_real_version_tag_rejects_floating_tags() -> None:
    for tag in ["latest", "stable", "edge", "beta", "dev", "main", "master", "nightly"]:
        assert is_real_version_tag(tag) is False


def test_is_real_version_tag_rejects_bare_arch_tag() -> None:
    assert is_real_version_tag("amd64") is False
    assert is_real_version_tag("arm64v8") is False


def test_is_real_version_tag_rejects_digit_leading_arch_tag() -> None:
    """"386" starts with a digit, so it would otherwise slip past the
    plain digit-leading shape check version_detect.is_version_tag() does —
    confirming this is caught independently."""
    assert is_real_version_tag("386") is False


def test_is_real_version_tag_rejects_arch_plus_floating_combo() -> None:
    assert is_real_version_tag("amd64-latest") is False


def test_is_real_version_tag_accepts_real_version_with_arch_suffix() -> None:
    """A real version tag that happens to carry an arch suffix must still
    be accepted — conservative/under-matching means never dropping a real
    version, only dropping pure noise."""
    assert is_real_version_tag("v1.2.3-amd64") is True


def test_filter_tags_default_keeps_only_real_versions() -> None:
    tags = ["latest", "stable", "amd64", "v1.0.0", "2.9.4", "sha-abc123"]
    assert filter_tags(tags) == ["v1.0.0", "2.9.4"]


def test_filter_tags_include_replaces_default_shape_check() -> None:
    """A tag scheme the default digit-leading check can't handle (e.g.
    "release-4.2.1") is exactly what tag_include exists to rescue — it
    must not be gated behind the default filter first."""
    tags = ["release-4.2.1", "release-4.1.0", "latest", "unrelated"]
    result = filter_tags(tags, tag_include=r"^release-\d")
    assert result == ["release-4.2.1", "release-4.1.0"]


def test_filter_tags_exclude_applies_on_top_of_default() -> None:
    tags = ["1.0.0", "1.0.0-rc1", "2.0.0"]
    result = filter_tags(tags, tag_exclude=r"-rc\d")
    assert result == ["1.0.0", "2.0.0"]


def test_filter_tags_exclude_applies_on_top_of_include() -> None:
    tags = ["release-4.2.1", "release-4.1.0-beta"]
    result = filter_tags(tags, tag_include=r"^release-", tag_exclude=r"beta")
    assert result == ["release-4.2.1"]


def test_filter_tags_invalid_include_regex_falls_back_to_default() -> None:
    tags = ["latest", "1.0.0"]
    result = filter_tags(tags, tag_include="[unclosed")
    assert result == ["1.0.0"]


def test_filter_tags_invalid_exclude_regex_is_ignored() -> None:
    tags = ["1.0.0", "2.0.0"]
    result = filter_tags(tags, tag_exclude="[unclosed")
    assert result == ["1.0.0", "2.0.0"]


def test_filter_tags_empty_list_returns_empty() -> None:
    assert filter_tags([]) == []


def test_is_real_version_tag_rejects_bare_all_digit_git_hash() -> None:
    """Confirmed real bug (ghcr.io/esphome/esphome): a short git commit
    hash tag that happens to be composed entirely of digits parses as a
    "valid" single-component PEP 440 version and would otherwise win the
    newest-tag comparison against every real release."""
    assert is_real_version_tag("9208227") is False


def test_is_real_version_tag_rejects_hex_git_hash_tags() -> None:
    for tag in ["002861f", "0059a6d", "01bbd04"]:
        assert is_real_version_tag(tag) is False


def test_is_real_version_tag_accepts_two_component_calendar_version() -> None:
    assert is_real_version_tag("2026.9") is True


def test_filter_tags_rejects_git_hash_tags_mixed_with_real_versions() -> None:
    tags = ["9208227", "002861f", "0059a6d", "2026.9.0b4", "2026.8.0"]
    assert filter_tags(tags) == ["2026.9.0b4", "2026.8.0"]


def test_select_newest_version_tag_sorts_version_aware_not_lexicographic() -> None:
    """The classic trap: '2.9.10' < '2.9.9' lexicographically, but 2.9.10
    is the newer release."""
    assert select_newest_version_tag(["2.9.9", "2.9.10", "2.9.2"]) == "2.9.10"


def test_select_newest_version_tag_handles_v_prefix() -> None:
    assert select_newest_version_tag(["v1.0.0", "v2.0.0", "v1.5.0"]) == "v2.0.0"


def test_select_newest_version_tag_handles_calendar_versioning() -> None:
    """Confirmed real case: ESPHome uses calendar versioning
    (YYYY.M.PATCH)."""
    assert select_newest_version_tag(["2025.12.1", "2026.9.1", "2026.1.0"]) == "2026.9.1"


def test_select_newest_version_tag_skips_unparseable_tags() -> None:
    assert select_newest_version_tag(["1.0.0", "not-a-version-at-all"]) == "1.0.0"


def test_select_newest_version_tag_empty_list_returns_none() -> None:
    assert select_newest_version_tag([]) is None


def test_select_newest_version_tag_all_unparseable_returns_none() -> None:
    assert select_newest_version_tag(["not-a-version", "also-not"]) is None


def test_select_newest_version_tag_excludes_dev_releases() -> None:
    """Confirmed real case: ghcr.io/esphome/esphome publishes a same-day
    dev build for every commit on main, which must never be shown as
    'latest' even though it has the highest raw version number."""
    tags = ["2026.9.0b4", "2026.10.0-dev20260914", "2026.9.0"]
    assert select_newest_version_tag(tags) == "2026.9.0"


def test_select_newest_version_tag_all_dev_returns_none() -> None:
    assert select_newest_version_tag(["1.0.0.dev1", "1.0.0.dev2"]) is None


def test_select_newest_version_tag_prefers_final_over_prerelease_same_line() -> None:
    tags = ["2026.9.0b1", "2026.9.0b2", "2026.9.0"]
    assert select_newest_version_tag(tags) == "2026.9.0"


def test_select_newest_version_tag_never_surfaces_prerelease_when_no_final_for_newest_line() -> None:
    """Revised rule (REGISTRY_TAG_WALK_SPEC.md's second amendment):
    pre-releases are never selectable as "latest" under any
    circumstance, even when the newest release line has no final yet —
    the older line's final is preferred over showing a beta at all."""
    tags = ["2026.8.0", "2026.9.0b1"]
    assert select_newest_version_tag(tags) == "2026.8.0"


def test_select_newest_version_tag_excludes_all_prereleases_regardless_of_type() -> None:
    tags = ["2026.8.0", "2026.9.0a1", "2026.9.0b1", "2026.9.0rc1"]
    assert select_newest_version_tag(tags) == "2026.8.0"


def test_select_newest_version_tag_returns_none_when_only_prereleases_exist() -> None:
    """A repository with no final releases published at all (only
    pre-releases/dev builds) must return nothing, not fall back to a
    pre-release."""
    tags = ["2026.9.0b1", "2026.9.0rc1", "2026.10.0-dev20260914"]
    assert select_newest_version_tag(tags) is None


def test_esphome_git_hash_tags_do_not_win_over_real_releases() -> None:
    """End-to-end regression for the exact reported production bug: a
    firehose of per-commit git-hash tags, some coincidentally all-digit,
    must never outrank the real newest final release. 2026.9.0b4 is a
    pre-release and must not be selected either, per the revised rule."""
    tags = ["9208227", "002861f", "0059a6d", "01bbd04", "2026.9.0b4", "2026.8.0"]
    candidates = filter_tags(tags)
    assert select_newest_version_tag(candidates) == "2026.8.0"


def test_frigate_style_no_real_version_tags_returns_none() -> None:
    """A service whose registry only ever publishes floating tags (no real
    version tags at all) must cleanly return nothing, never a false match
    on 'stable'/'latest' itself."""
    tags = ["stable", "latest", "dev"]
    assert filter_tags(tags) == []
    assert select_newest_version_tag(filter_tags(tags)) is None


def test_rank_final_versions_orders_newest_first() -> None:
    """Used by tag_walk_coordinator.py's bounded pull-target-version
    search (takes the top N of this list) — must be newest-first, not
    just correctly sorted in some direction."""
    tags = ["2.9.9", "2.9.10", "2.9.2"]
    assert rank_final_versions(tags) == ["2.9.10", "2.9.9", "2.9.2"]


def test_rank_final_versions_excludes_dev_and_prerelease() -> None:
    tags = ["2026.8.0", "2026.9.0b1", "2026.10.0-dev20260914"]
    assert rank_final_versions(tags) == ["2026.8.0"]


def test_rank_final_versions_skips_unparseable_tags() -> None:
    assert rank_final_versions(["1.0.0", "not-a-version"]) == ["1.0.0"]


def test_rank_final_versions_empty_input_returns_empty() -> None:
    assert rank_final_versions([]) == []


def test_rank_final_versions_all_excluded_returns_empty() -> None:
    assert rank_final_versions(["1.0.0b1", "1.0.0.dev1"]) == []


def test_rank_final_versions_tie_break_prefers_more_specific_regardless_of_input_order() -> None:
    """"2.9" and "2.9.0" compare as the exact same Version (packaging pads
    the shorter release tuple with zeros) — the more complete/specific
    form must win deterministically, not depend on which one the
    registry happened to list first."""
    assert rank_final_versions(["2.9", "2.9.0"])[0] == "2.9.0"
    assert rank_final_versions(["2.9.0", "2.9"])[0] == "2.9.0"


def test_rank_final_versions_tie_break_never_beats_a_real_newer_version() -> None:
    tags = ["2.10", "2.9.0", "2.9"]
    assert rank_final_versions(tags) == ["2.10", "2.9.0", "2.9"]


def test_select_newest_version_tag_matches_rank_final_versions_head() -> None:
    """select_newest_version_tag() must always agree with
    rank_final_versions()[0] — they're required to share one ranking
    rule, not two independently-maintained ones (see
    REGISTRY_TAG_WALK_SPEC.md's tie-break consistency requirement)."""
    tags = ["2.9.9", "2.9.10", "2.9.2", "3.0.0b1", "3.1.0-dev1"]
    assert select_newest_version_tag(tags) == rank_final_versions(tags)[0]
