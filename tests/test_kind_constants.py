from ha_docker_compose import const


def _kind_constants() -> dict[str, str]:
    return {name: value for name, value in vars(const).items() if name.startswith("KIND_")}


def test_every_kind_constant_value_is_distinct() -> None:
    kinds = _kind_constants()
    assert len(kinds) > 0
    assert len(set(kinds.values())) == len(kinds), f"duplicate kind values among: {kinds}"


def test_kind_constant_values_match_their_name() -> None:
    # KIND_STACK_STATE = "stack_state" — catches a copy/paste value that
    # doesn't match its own constant name.
    for name, value in _kind_constants().items():
        assert value == name[len("KIND_"):].lower(), (name, value)
