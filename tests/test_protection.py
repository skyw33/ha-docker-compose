from ha_docker_compose.protection import (
    is_service_protected,
    is_stack_protected,
    parse_compose_labels,
)


def test_parse_compose_labels_list_form() -> None:
    service_def = {"labels": ["ha_docker_compose.protection=full", "other.label=x"]}
    assert parse_compose_labels(service_def) == {
        "ha_docker_compose.protection": "full",
        "other.label": "x",
    }


def test_parse_compose_labels_map_form() -> None:
    service_def = {"labels": {"ha_docker_compose.protection": "full"}}
    assert parse_compose_labels(service_def) == {"ha_docker_compose.protection": "full"}


def test_parse_compose_labels_missing() -> None:
    assert parse_compose_labels({}) == {}


def test_parse_compose_labels_empty_list() -> None:
    assert parse_compose_labels({"labels": []}) == {}


def test_parse_compose_labels_ignores_malformed_list_entries() -> None:
    service_def = {"labels": ["not-a-key-value-pair", "real.key=value"]}
    assert parse_compose_labels(service_def) == {"real.key": "value"}


def test_is_service_protected_true_for_full() -> None:
    service_def = {"labels": ["ha_docker_compose.protection=full"]}
    assert is_service_protected(service_def) is True


def test_is_service_protected_false_when_unset() -> None:
    assert is_service_protected({}) is False
    assert is_service_protected({"image": "nginx"}) is False


def test_is_service_protected_false_for_other_values() -> None:
    service_def = {"labels": ["ha_docker_compose.protection=no-stop"]}
    assert is_service_protected(service_def) is False


def test_is_stack_protected_true_when_any_service_is_full() -> None:
    compose_config = {
        "services": {
            "docker-socket-proxy": {"labels": ["ha_docker_compose.protection=full"]},
            "docker-cli": {"labels": ["ha_docker_compose.protection=full"]},
        }
    }
    assert is_stack_protected(compose_config) is True


def test_is_stack_protected_true_when_only_one_service_is_full() -> None:
    compose_config = {
        "services": {
            "protected-one": {"labels": ["ha_docker_compose.protection=full"]},
            "ordinary-one": {"image": "nginx"},
        }
    }
    assert is_stack_protected(compose_config) is True


def test_is_stack_protected_false_when_no_service_is_protected() -> None:
    compose_config = {"services": {"homeassistant": {"image": "ghcr.io/home-assistant/home-assistant"}}}
    assert is_stack_protected(compose_config) is False


def test_is_stack_protected_false_for_empty_services() -> None:
    assert is_stack_protected({"services": {}}) is False
    assert is_stack_protected({}) is False
