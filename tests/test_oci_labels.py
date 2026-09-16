from ha_docker_compose.oci_labels import resolve_label


def test_resolve_label_image_wins_when_present() -> None:
    value, source = resolve_label(
        {"the.label": "from-image"}, {"the.label": "from-container"}, "the.label"
    )
    assert (value, source) == ("from-image", "image")


def test_resolve_label_falls_back_to_container() -> None:
    value, source = resolve_label({}, {"the.label": "from-container"}, "the.label")
    assert (value, source) == ("from-container", "container")


def test_resolve_label_none_when_container_labels_is_none() -> None:
    value, source = resolve_label({}, None, "the.label")
    assert (value, source) == (None, None)


def test_resolve_label_none_when_neither_has_it() -> None:
    value, source = resolve_label({"other": "x"}, {"other": "y"}, "the.label")
    assert (value, source) == (None, None)
