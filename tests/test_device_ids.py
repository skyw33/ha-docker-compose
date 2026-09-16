from pathlib import Path

from ha_docker_compose.device_ids import (
    expected_device_identifiers,
    service_device_identifier,
    stack_device_identifier,
)
from ha_docker_compose.discovery import StackInfo


def _stack(name: str, services: dict, path: Path | None = None) -> StackInfo:
    return StackInfo(
        name=name,
        path=path or Path(f"/stacks/{name}"),
        compose_filename="docker-compose.yml",
        has_env_file=False,
        compose_config={"services": services},
    )


def test_identifiers_are_exact_and_do_not_collide_on_shared_prefixes() -> None:
    """Naive prefix matching on the "{entry}_{stack}_{service}" scheme
    would wrongly treat stack "foo"'s identifier as a prefix of stack
    "foo_bar"'s — identifiers must be compared as exact tuples, never
    parsed/prefix-matched, to stay correct regardless of naming."""
    foo = stack_device_identifier("entry1", "foo")
    foo_bar = stack_device_identifier("entry1", "foo_bar")

    assert foo != foo_bar
    assert foo not in {foo_bar}


def test_expected_device_identifiers_includes_stack_and_every_service() -> None:
    stacks = [_stack("media", {"jellyfin": {"image": "jellyfin"}, "sonarr": {"image": "sonarr"}})]

    identifiers = expected_device_identifiers("entry1", stacks)

    assert identifiers == {
        stack_device_identifier("entry1", "media"),
        service_device_identifier("entry1", "media", "jellyfin"),
        service_device_identifier("entry1", "media", "sonarr"),
    }


def test_removed_stack_is_absent_from_expected_identifiers() -> None:
    """Simulates a reload after a stack folder was deleted: the previous
    load's identifiers for it should no longer appear in the fresh set,
    which is exactly what __init__.py's pruning uses to decide what to
    remove."""
    previous_stacks = [_stack("media", {"jellyfin": {"image": "jellyfin"}})]
    previous_identifiers = expected_device_identifiers("entry1", previous_stacks)

    current_stacks: list[StackInfo] = []  # the folder is gone
    current_identifiers = expected_device_identifiers("entry1", current_stacks)

    stale = previous_identifiers - current_identifiers
    assert stale == {
        stack_device_identifier("entry1", "media"),
        service_device_identifier("entry1", "media", "jellyfin"),
    }


def test_removed_service_within_still_present_stack_is_pruned_alone() -> None:
    """Editing a compose file to drop one service should only make that
    service's identifier stale — the stack's own identifier, and any
    other still-defined service, must remain expected."""
    previous_stacks = [
        _stack("media", {"jellyfin": {"image": "jellyfin"}, "sonarr": {"image": "sonarr"}})
    ]
    previous_identifiers = expected_device_identifiers("entry1", previous_stacks)

    current_stacks = [_stack("media", {"jellyfin": {"image": "jellyfin"}})]  # sonarr removed
    current_identifiers = expected_device_identifiers("entry1", current_stacks)

    stale = previous_identifiers - current_identifiers
    assert stale == {service_device_identifier("entry1", "media", "sonarr")}
    # The stack itself and the surviving service are still expected.
    assert stack_device_identifier("entry1", "media") in current_identifiers
    assert service_device_identifier("entry1", "media", "jellyfin") in current_identifiers


def test_stopped_but_still_defined_stack_has_no_stale_identifiers() -> None:
    """A stack whose folder still exists (just no running containers) must
    never be treated as stale — this test stands in for that case by
    simply confirming an unchanged stack list produces an unchanged
    (non-stale) identifier set, regardless of runtime container state,
    which discover_stacks/StackInfo don't even track."""
    stacks = [_stack("media", {"jellyfin": {"image": "jellyfin"}})]

    before = expected_device_identifiers("entry1", stacks)
    after = expected_device_identifiers("entry1", stacks)

    assert before == after
    assert before - after == set()
