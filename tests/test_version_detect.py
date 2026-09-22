from ha_docker_compose.version_detect import (
    UNVERIFIED_SUFFIX,
    detect_version,
    has_version_structure,
    is_version_tag,
    normalize_version,
)


def test_is_version_tag_accepts_bare_digit() -> None:
    assert is_version_tag("2.9.4") is True


def test_is_version_tag_accepts_v_prefixed() -> None:
    assert is_version_tag("v4.2.0") is True


def test_is_version_tag_rejects_known_floating_tags() -> None:
    for tag in ["latest", "stable", "edge", "beta", "dev", "main", "master", "nightly"]:
        assert is_version_tag(tag) is False


def test_is_version_tag_rejects_floating_tags_case_insensitively() -> None:
    assert is_version_tag("Latest") is False
    assert is_version_tag("STABLE") is False


def test_is_version_tag_rejects_non_numeric_start() -> None:
    assert is_version_tag("rc1") is False
    assert is_version_tag("") is False


def test_normalize_version_strips_leading_v() -> None:
    assert normalize_version("v4.2.0") == "4.2.0"


def test_normalize_version_leaves_bare_digit_untouched() -> None:
    assert normalize_version("2.9.4") == "2.9.4"


def test_normalize_version_strips_package_prefix() -> None:
    assert normalize_version("n8n@2.9.4") == "2.9.4"


def test_detect_version_prefers_image_label_over_tag() -> None:
    result = detect_version(
        "grocy/backend:v4.2.0",
        image_labels={"org.opencontainers.image.version": "4.2.0-real"},
        container_labels=None,
    )
    assert result == "4.2.0-real"


def test_detect_version_falls_back_to_container_label() -> None:
    result = detect_version(
        "someimage:latest",
        image_labels={},
        container_labels={"org.opencontainers.image.version": "9.9.9"},
    )
    assert result == "9.9.9"


def test_detect_version_falls_back_to_version_tag_when_no_label() -> None:
    result = detect_version("grocy/backend:v4.2.0", image_labels={}, container_labels=None)
    assert result == "4.2.0"


def test_detect_version_bare_digit_tag_no_label() -> None:
    result = detect_version("n8nio/n8n:2.9.4", image_labels={}, container_labels=None)
    assert result == "2.9.4"


def test_detect_version_none_for_floating_tag_no_label() -> None:
    """Frigate's own scenario: :stable tag, no version label anywhere."""
    result = detect_version("ghcr.io/blakeblackshear/frigate:stable", image_labels={}, container_labels=None)
    assert result is None


def test_detect_version_none_for_latest_tag_no_label() -> None:
    result = detect_version("someimage:latest", image_labels={}, container_labels={})
    assert result is None


def test_detect_version_ignores_floating_tag_style_label_value() -> None:
    """Real-world case: twinproduction/gatus:latest's own image config sets
    org.opencontainers.image.version="latest" (an upstream CI metadata bug
    that passes the tag name through as the version label). A label saying
    "latest" is exactly as unhelpful as a tag saying it and must not be
    trusted just because it came from a label."""
    result = detect_version(
        "twinproduction/gatus:latest",
        image_labels={"org.opencontainers.image.version": "latest"},
        container_labels=None,
    )
    assert result is None


def test_detect_version_falls_back_to_tag_when_label_is_floating_tag_style() -> None:
    """If the label is junk but the tag itself is a real version, the tag
    should still be used — the label being bad doesn't poison the whole
    lookup, it just gets skipped."""
    result = detect_version(
        "someimage:v4.2.0",
        image_labels={"org.opencontainers.image.version": "stable"},
        container_labels=None,
    )
    assert result == "4.2.0"


def test_detect_version_real_version_label_still_wins() -> None:
    """Confirms the fix doesn't overcorrect: a genuine version label is
    still used verbatim and still takes priority over the tag."""
    result = detect_version(
        "grocy/backend:v4.2.0",
        image_labels={"org.opencontainers.image.version": "4.2.0"},
        container_labels=None,
    )
    assert result == "4.2.0"


def test_detect_version_uses_verified_current_version_when_no_label_or_tag() -> None:
    """Confirmed real case: ghcr.io/blakeblackshear/frigate:stable — empty
    image labels (verified directly against the registry, not assumed),
    a floating tag with nothing to parse. verified_current_version (a
    live digest cross-reference computed by TagWalkCoordinator) is the
    only remaining source, and — unlike assumed_version — is returned
    verbatim, no unverified suffix."""
    result = detect_version(
        "ghcr.io/blakeblackshear/frigate:stable",
        image_labels={},
        container_labels=None,
        verified_current_version="0.18.0",
    )
    assert result == "0.18.0"


def test_detect_version_verified_current_version_never_overrides_real_label() -> None:
    result = detect_version(
        "grocy/backend:v4.2.0",
        image_labels={"org.opencontainers.image.version": "4.2.0"},
        container_labels=None,
        verified_current_version="9.9.9",
    )
    assert result == "4.2.0"


def test_detect_version_verified_current_version_never_overrides_real_tag() -> None:
    result = detect_version(
        "someimage:v4.2.0",
        image_labels={},
        container_labels=None,
        verified_current_version="9.9.9",
    )
    assert result == "4.2.0"


def test_detect_version_verified_current_version_wins_over_assumed_version() -> None:
    """A live digest match outranks the old, stale, unverified
    last-pull snapshot — this is the priority ordering the whole feature
    is for: a verified fact should never lose to an assumption just
    because the assumption happened to be checked first historically."""
    result = detect_version(
        "ghcr.io/blakeblackshear/frigate:stable",
        image_labels={},
        container_labels=None,
        verified_current_version="0.18.0",
        assumed_version="0.17.0",
    )
    assert result == "0.18.0"


def test_detect_version_empty_verified_current_version_falls_through() -> None:
    """An empty string (falsy) must fall through to assumed_version, not
    be returned as-is or block the chain."""
    result = detect_version(
        "someimage:latest",
        image_labels={},
        container_labels=None,
        verified_current_version="",
        assumed_version="v0.3.0",
    )
    assert result == "v0.3.0" + UNVERIFIED_SUFFIX


def test_detect_version_falls_back_to_assumed_version_when_nothing_else_available() -> None:
    """Confirmed real case: otbr — no OCI label, floating tag pin. The
    persisted assumed_version (from the last successful pull's
    latest_registry_tag) is the only remaining source, and must be
    suffixed as unverified."""
    result = detect_version(
        "otbr:latest",
        image_labels={},
        container_labels=None,
        assumed_version="v0.3.0",
    )
    assert result == "v0.3.0" + UNVERIFIED_SUFFIX


def test_detect_version_assumed_version_never_overrides_real_label() -> None:
    """The unverified fallback must never fire when the real chain
    already succeeded — a real label always wins, suffix or not."""
    result = detect_version(
        "grocy/backend:v4.2.0",
        image_labels={"org.opencontainers.image.version": "4.2.0"},
        container_labels=None,
        assumed_version="9.9.9",
    )
    assert result == "4.2.0"


def test_detect_version_assumed_version_never_overrides_real_tag() -> None:
    """Nor does it override a real, pinned-tag-derived result — only fires
    when every prior check in the chain returns nothing at all."""
    result = detect_version(
        "someimage:v4.2.0",
        image_labels={},
        container_labels=None,
        assumed_version="9.9.9",
    )
    assert result == "4.2.0"


def test_detect_version_no_assumed_version_stays_none() -> None:
    """No regression for stacks that don't hit this specific gap: with no
    assumed_version supplied (never successfully pulled since this
    feature shipped), behavior is unchanged from before this feature."""
    result = detect_version("someimage:latest", image_labels={}, container_labels={})
    assert result is None


def test_detect_version_empty_assumed_version_stays_none() -> None:
    """An empty string (falsy) must not produce a bogus ' (unverified)'
    suffix on nothing."""
    result = detect_version(
        "someimage:latest", image_labels={}, container_labels={}, assumed_version=""
    )
    assert result is None


def test_has_version_structure_requires_two_dot_separated_components() -> None:
    assert has_version_structure("2.9.4") is True
    assert has_version_structure("2026.9") is True
    assert has_version_structure("v4.2.0") is True


def test_has_version_structure_rejects_bare_word() -> None:
    assert has_version_structure("weekly") is False
    assert has_version_structure("latest") is False


def test_has_version_structure_rejects_bare_integer() -> None:
    assert has_version_structure("9208227") is False


def test_detect_version_rejects_non_version_label_not_in_word_list() -> None:
    """Confirmed real case: onstar2mqtt's image sets
    org.opencontainers.image.version to "weekly" (a release-cadence name,
    not a version, and not one of the known floating-tag words) — the
    same category of bug as gatus's "latest" label, but a word the
    original NON_VERSION_TAGS list never covered. The structural check
    catches it without needing "weekly" added to any list."""
    result = detect_version(
        "onstar2mqtt:latest",
        image_labels={"org.opencontainers.image.version": "weekly"},
        container_labels=None,
    )
    assert result is None


def test_detect_version_falls_back_to_tag_when_label_is_arbitrary_non_version_word() -> None:
    """If the label is junk (any non-version word, not just a known
    floating-tag one) but the tag itself is a real version, the tag
    should still be used — matches the existing precedent for known
    floating-tag-style label values."""
    result = detect_version(
        "someimage:v4.2.0",
        image_labels={"org.opencontainers.image.version": "weekly"},
        container_labels=None,
    )
    assert result == "4.2.0"


def test_detect_version_rejects_single_component_numeric_label() -> None:
    """A label value that's just a bare number (no dot-separated
    structure) is rejected too, same as a bare-word placeholder —
    consistent with is_real_version_tag()'s registry-tag-walking
    equivalent for the identical category of value."""
    result = detect_version(
        "someimage:latest",
        image_labels={"org.opencontainers.image.version": "7"},
        container_labels=None,
    )
    assert result is None
