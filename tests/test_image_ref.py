from ha_docker_compose.image_ref import ParsedImageRef, parse_image_ref, repo_name, tag


def test_repo_name_simple() -> None:
    assert repo_name("nginx") == "nginx"
    assert repo_name("nginx:latest") == "nginx"
    assert repo_name("postgres:16.4") == "postgres"


def test_repo_name_with_registry_port() -> None:
    assert repo_name("myregistry:5000/app:1.0") == "myregistry:5000/app"
    assert repo_name("myregistry:5000/app") == "myregistry:5000/app"


def test_repo_name_with_digest() -> None:
    assert repo_name("nginx@sha256:abc123") == "nginx"


def test_tag_simple() -> None:
    assert tag("nginx") == "latest"
    assert tag("nginx:latest") == "latest"
    assert tag("postgres:16.4") == "16.4"


def test_tag_with_registry_port() -> None:
    assert tag("myregistry:5000/app:1.0") == "1.0"
    assert tag("myregistry:5000/app") == "latest"


def test_tag_with_digest_falls_back_to_default() -> None:
    assert tag("nginx@sha256:abc123") == "latest"


def test_parse_docker_hub_official_with_tag() -> None:
    assert parse_image_ref("nginx:1.27") == ParsedImageRef("docker.io", "library/nginx", "1.27")


def test_parse_docker_hub_official_latest() -> None:
    assert parse_image_ref("nginx") == ParsedImageRef("docker.io", "library/nginx", "latest")


def test_parse_docker_hub_namespaced() -> None:
    assert parse_image_ref("linuxserver/something:latest") == ParsedImageRef(
        "docker.io", "linuxserver/something", "latest"
    )


def test_parse_docker_hub_explicit_library_prefix() -> None:
    assert parse_image_ref("library/nginx:latest") == ParsedImageRef(
        "docker.io", "library/nginx", "latest"
    )


def test_parse_ghcr_with_tag() -> None:
    assert parse_image_ref("ghcr.io/owner/repo:tag") == ParsedImageRef("ghcr.io", "owner/repo", "tag")


def test_parse_ghcr_no_tag() -> None:
    assert parse_image_ref("ghcr.io/owner/repo") == ParsedImageRef("ghcr.io", "owner/repo", "latest")


def test_parse_bare_redis_resolves_to_docker_hub_library() -> None:
    assert parse_image_ref("redis") == ParsedImageRef("docker.io", "library/redis", "latest")


def test_parse_private_registry_with_port() -> None:
    assert parse_image_ref("myregistry:5000/app:1.0") == ParsedImageRef(
        "myregistry:5000", "app", "1.0"
    )
