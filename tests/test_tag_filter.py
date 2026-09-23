from ha_docker_compose.tag_filter import (
    KNOWN_BASE_IMAGE_SUFFIXES,
    _strip_known_base_image_suffix,
    classify_digest_search_result,
    filter_tags,
    is_not_behind,
    is_real_version_tag,
    known_base_image_suffix,
    prioritize_by_suffix,
    rank_final_versions,
    select_newest_version_tag,
)
from ha_docker_compose.version_detect import NO_RELEASE_MATCH


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


def test_is_not_behind_true_when_digests_match() -> None:
    assert is_not_behind("sha256:aaa", "sha256:aaa") is True


def test_is_not_behind_false_when_digests_differ() -> None:
    assert is_not_behind("sha256:aaa", "sha256:bbb") is False


def test_is_not_behind_false_when_pinned_digest_missing() -> None:
    assert is_not_behind(None, "sha256:aaa") is False


def test_is_not_behind_false_when_local_digest_missing() -> None:
    assert is_not_behind("sha256:aaa", None) is False


def test_is_not_behind_false_when_both_missing() -> None:
    assert is_not_behind(None, None) is False


def test_select_newest_version_tag_matches_rank_final_versions_head() -> None:
    """select_newest_version_tag() must always agree with
    rank_final_versions()[0] — they're required to share one ranking
    rule, not two independently-maintained ones (see
    REGISTRY_TAG_WALK_SPEC.md's tie-break consistency requirement)."""
    tags = ["2.9.9", "2.9.10", "2.9.2", "3.0.0b1", "3.1.0-dev1"]
    assert select_newest_version_tag(tags) == rank_final_versions(tags)[0]


# --- _strip_known_base_image_suffix / base-image-suffix ranking ------------


def test_strip_known_base_image_suffix_alpine() -> None:
    assert _strip_known_base_image_suffix("2.1.2-alpine") == "2.1.2"


def test_strip_known_base_image_suffix_case_insensitive() -> None:
    assert _strip_known_base_image_suffix("2.1.2-Alpine") == "2.1.2"
    assert _strip_known_base_image_suffix("2.1.2-ALPINE") == "2.1.2"


def test_strip_known_base_image_suffix_none_for_unknown_word() -> None:
    """The whole point: only a curated list of real distro/base-image
    identifiers, not "any word Version() doesn't recognize" — confirmed
    real risk this guards against: eclipse-mosquitto-shaped repos could
    just as easily publish a "-nightly" or "-snapshot" tag, which must
    never be treated as a clean final release."""
    assert _strip_known_base_image_suffix("2.2.0-nightly") is None
    assert _strip_known_base_image_suffix("2.2.0-snapshot") is None
    assert _strip_known_base_image_suffix("2.2.0-unstable") is None


def test_strip_known_base_image_suffix_none_when_prefix_itself_invalid() -> None:
    assert _strip_known_base_image_suffix("not-a-version-alpine") is None


def test_strip_known_base_image_suffix_none_for_recognized_prerelease_keyword() -> None:
    """A real pre-release tag must never be treated as strippable — it
    already parses correctly (as a pre-release, excluded from final
    ranking on its own terms), so this function is never even reached
    for it inside rank_final_versions()'s try/except; this test locks in
    that none of the curated words collide with packaging's own
    pre/post/dev-release keywords, so that invariant can't quietly break
    if the allowlist is ever extended."""
    prerelease_keywords = {"alpha", "a", "beta", "b", "preview", "pre", "c", "rc", "post", "rev", "r", "dev"}
    assert KNOWN_BASE_IMAGE_SUFFIXES.isdisjoint(prerelease_keywords)


def test_strip_known_base_image_suffix_none_for_multiple_stacked_suffixes() -> None:
    """Documented coverage gap, not a correctness risk (this module
    prefers under- over over-matching throughout) — see the function's
    own docstring."""
    assert _strip_known_base_image_suffix("3.12-slim-bookworm") is None


def test_strip_known_base_image_suffix_none_for_tag_with_no_suffix() -> None:
    assert _strip_known_base_image_suffix("2.1.2") is None


def test_rank_final_versions_promotes_base_image_suffixed_tag() -> None:
    """The confirmed real case: eclipse-mosquitto's 2.1.x line is only
    ever published as "<version>-alpine" — no bare tag exists at all.
    The *original* tag string must be what's returned, never a
    synthesized bare version — a caller needs the real, pullable tag
    name to look it up on the registry."""
    tags = ["2.0.22", "2.0.21", "2.1.2-alpine", "2.1.1-alpine", "2.1.0-alpine"]
    assert rank_final_versions(tags)[0] == "2.1.2-alpine"
    assert select_newest_version_tag(tags) == "2.1.2-alpine"


def test_rank_final_versions_does_not_promote_nightly_suffixed_tag() -> None:
    """The false-positive case this whole design exists to avoid: a
    "-nightly"-suffixed tag must never be promoted to "latest final
    release", even though it would fail direct Version() parsing the
    same way a real base-image-suffixed tag does."""
    tags = ["2.0.22", "2.1.0", "2.2.0-nightly"]
    assert "2.2.0-nightly" not in rank_final_versions(tags)
    assert select_newest_version_tag(tags) == "2.1.0"


def test_rank_final_versions_mosquitto_real_world_tag_shape() -> None:
    """End-to-end against the real, confirmed-live eclipse-mosquitto
    shape (older lines: bare + "-openssl" variant of the same version;
    2.1.x line: "-alpine" only, no bare tag) — proves the fix resolves
    the actually-reported symptom (latest_registry_tag showing 2.0.22
    instead of 2.1.2), not just the isolated helper function."""
    tags = [
        "2.0.22",
        "2.0.22-openssl",
        "2.0.21",
        "2.0.21-openssl",
        "2.1.2-alpine",
        "2.1.1-alpine",
        "2.1.0-alpine",
        "2.1-alpine",
        "latest",
        "alpine",
    ]
    assert select_newest_version_tag(tags) == "2.1.2-alpine"


def test_rank_final_versions_promotes_build_variant_suffixed_tag() -> None:
    """The confirmed real case: docker:cli is digest-DIFFERENT from bare
    docker:<version> (a genuine image split, not an alias) — "cli" must
    be recognized the same way "alpine" is, or a service pinned to
    "29.8.1-cli" can never even enter the ranked candidate list."""
    for suffix in ("cli", "dind", "windowsservercore", "git", "rootless"):
        tags = ["29.8.0", f"29.8.1-{suffix}", "29.8.0-cli"]
        assert f"29.8.1-{suffix}" in rank_final_versions(tags)


def test_known_base_image_suffix_recognizes_build_variant() -> None:
    assert known_base_image_suffix("29.8.1-cli") == "cli"
    assert known_base_image_suffix("2.1.2-alpine") == "alpine"


def test_known_base_image_suffix_none_for_bare_tag() -> None:
    assert known_base_image_suffix("29.8.1") is None


def test_known_base_image_suffix_none_for_unrecognized_word() -> None:
    assert known_base_image_suffix("2.2.0-nightly") is None


def test_prioritize_by_suffix_noop_when_pinned_suffix_none() -> None:
    ranked = ["29.8.1", "29.8.1-cli", "29.8.0"]
    assert prioritize_by_suffix(ranked, None) == ranked


def test_prioritize_by_suffix_same_suffix_candidate_beats_equal_ranked_bare_tag() -> None:
    """The exact docker:cli mechanism: a service pinned to "29.8.1-cli"
    needs its own suffix's candidates searched before same-ranked bare
    tags or other variants, since only a same-suffix candidate can ever
    share its digest."""
    ranked = rank_final_versions(
        ["29.8.1", "29.8.1-cli", "29.8.1-dind", "29.8.0", "29.8.0-cli"]
    )
    prioritized = prioritize_by_suffix(ranked, "cli")
    assert prioritized[0] == "29.8.1-cli"
    assert prioritized.index("29.8.1-cli") < prioritized.index("29.8.1")
    assert prioritized.index("29.8.0-cli") < prioritized.index("29.8.1-dind")


def test_prioritize_by_suffix_preserves_relative_order_within_each_group() -> None:
    """A reorder (stable partition), not a re-sort — within the
    same-suffix group and within the "other" group, newest-first order
    from rank_final_versions() must survive untouched."""
    ranked = rank_final_versions(
        ["29.8.1", "29.8.1-cli", "29.8.0", "29.8.0-cli", "29.7.0", "29.7.0-cli"]
    )
    prioritized = prioritize_by_suffix(ranked, "cli")
    assert prioritized == ["29.8.1-cli", "29.8.0-cli", "29.7.0-cli", "29.8.1", "29.8.0", "29.7.0"]


def test_prioritize_by_suffix_finds_older_same_suffix_candidate_ahead_of_newer_other_suffix_ties() -> (
    None
):
    """Reordering before bounding matters: a service several versions
    behind on its own suffix must not have its match pushed out of a
    bounded search window by ties of newer versions in other suffixes."""
    ranked = rank_final_versions(
        [
            "29.8.1",
            "29.8.1-cli",
            "29.8.1-dind",
            "29.8.0",
            "29.8.0-cli",
            "29.8.0-dind",
            "29.5.0",
            "29.5.0-cli",
        ]
    )
    prioritized = prioritize_by_suffix(ranked, "cli")[:3]
    assert prioritized == ["29.8.1-cli", "29.8.0-cli", "29.5.0-cli"]


def test_rank_final_versions_docker_cli_real_world_tag_shape() -> None:
    """End-to-end against the real, confirmed-live docker/docker tag
    shape: bare tags and cli/dind/windowsservercore variants of the same
    versions coexist, with the bare tag genuinely being the newest
    release. latest_registry_tag stays variant-agnostic (still bare, per
    REGISTRY_TAG_WALK_SPEC.md's build-variant-suffix amendment) while a
    :cli-pinned service's prioritized search correctly surfaces its own
    variant first."""
    tags = [
        "29.8.1",
        "29.8.1-cli",
        "29.8.1-dind",
        "29.8.1-windowsservercore",
        "29.8.0",
        "29.8.0-cli",
        "29.8.0-dind",
        "29.8.0-windowsservercore",
    ]
    assert select_newest_version_tag(tags) == "29.8.1"

    ranked = rank_final_versions(tags)
    pinned_suffix = known_base_image_suffix("29.8.1-cli")
    prioritized = prioritize_by_suffix(ranked, pinned_suffix)
    assert prioritized[0] == "29.8.1-cli"


def test_classify_digest_search_result_matched_tag_wins() -> None:
    assert classify_digest_search_result(["2.10.1", "2.10.0"], "2.10.1", True) == "2.10.1"


def test_classify_digest_search_result_empty_ranked_is_case_a_nothing_to_search() -> None:
    """Case (a): no candidates existed to search among at all — stays
    None, the existing "nothing to compare" behavior, never
    NO_RELEASE_MATCH. This is also the shape of the Gatus local_digest
    gap from a *different*, separately-tracked investigation: whenever
    there's no digest to search against in the first place,
    _search_ranked_for_digest() is never even called (see
    tag_walk_coordinator.py's `if local_digest:`/`if pinned_digest:`
    guards) — this stays case (a) regardless of *why* the digest was
    missing."""
    assert classify_digest_search_result([], None, False) is None
    assert classify_digest_search_result([], None, True) is None


def test_classify_digest_search_result_exhausted_with_no_match_is_unreleased() -> None:
    """Case (b): a real search ran (at least one candidate's digest was
    actually fetched and compared) and genuinely found nothing — the
    bounded-search-exhausted case."""
    assert classify_digest_search_result(["2.10.1", "2.10.0"], None, True) is NO_RELEASE_MATCH


def test_classify_digest_search_result_all_lookups_failed_stays_none() -> None:
    """Every candidate's manifest lookup errored out (e.g. a registry
    outage mid-search) — a degraded search that learned nothing, not a
    confirmed non-match. Must stay None, never a false NO_RELEASE_MATCH
    claim — the same under-matching bias this module applies everywhere
    else (module docstring)."""
    assert classify_digest_search_result(["2.10.1", "2.10.0"], None, False) is None


def test_onstar2mqtt_real_world_ahead_of_latest_release_is_unreleased() -> None:
    """End-to-end, modeled on the confirmed real onstar2mqtt case: pinned
    to a floating tag, no usable version label ("weekly" — see
    version_detect.py), the newest tagged release is v2.10.1, and
    GitHub's own compare view confirms the running build is 20 commits
    ahead of it — a real search against real ranked candidates, correctly
    finding no digest match at all."""
    tags = ["v2.10.1", "v2.10.0", "v2.9.0", "v2.8.4", "v2.8.2", "v2.8.1"]
    ranked = rank_final_versions(tags)
    assert ranked[0] == "v2.10.1"
    result = classify_digest_search_result(ranked, None, True)
    assert result is NO_RELEASE_MATCH
