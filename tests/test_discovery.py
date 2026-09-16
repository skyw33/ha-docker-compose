from pathlib import Path

import pytest

from ha_docker_compose.discovery import discover_stacks


@pytest.mark.asyncio
async def test_discover_stacks(tmp_path: Path) -> None:
    (tmp_path / "stack_1").mkdir()
    (tmp_path / "stack_1" / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx:latest\n"
    )

    (tmp_path / "stack_2").mkdir()
    (tmp_path / "stack_2" / "compose.yaml").write_text(
        "name: custom-name\n"
        "services:\n"
        "  app:\n"
        "    image: python:3.12\n"
        "  redis:\n"
        "    image: redis:7\n"
    )

    (tmp_path / "stack_3").mkdir()
    (tmp_path / "stack_3" / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: postgres:16.4\n"
    )
    (tmp_path / "stack_3" / ".env").write_text("FOO=bar\n")

    (tmp_path / "not_a_stack").mkdir()
    (tmp_path / "not_a_stack" / "readme.txt").write_text("nothing here")

    (tmp_path / "a_loose_file.txt").write_text("should be ignored, not a dir")

    stacks = await discover_stacks(tmp_path)
    by_folder = {s.path.name: s for s in stacks}

    assert len(stacks) == 3

    assert by_folder["stack_1"].name == "stack_1"
    assert by_folder["stack_1"].has_env_file is False
    assert by_folder["stack_1"].service_names == ["web"]

    # A compose-defined top-level `name:` takes precedence over the folder name.
    assert by_folder["stack_2"].name == "custom-name"
    assert sorted(by_folder["stack_2"].service_names) == ["app", "redis"]

    assert by_folder["stack_3"].name == "stack_3"
    assert by_folder["stack_3"].has_env_file is True


@pytest.mark.asyncio
async def test_discover_stacks_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await discover_stacks(tmp_path / "does_not_exist")


@pytest.mark.asyncio
async def test_discover_stacks_empty_root_returns_empty_list(tmp_path: Path) -> None:
    assert await discover_stacks(tmp_path) == []


@pytest.mark.asyncio
async def test_discover_stacks_uses_compose_executor_when_provided(tmp_path: Path) -> None:
    """discover_stacks should prefer a supplied ComposeExecutor's resolved
    config (sidecar-exec'd `docker compose config`) over the raw YAML
    fallback used when none is supplied (e.g. during config-flow
    validation, before a ComposeExecutor exists)."""
    (tmp_path / "stack_1").mkdir()
    (tmp_path / "stack_1" / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx:latest\n"
    )

    class FakeComposeExecutor:
        async def get_compose_config(self, stack_dir: Path) -> dict:
            return {"name": "renamed-stack", "services": {"web": {"image": "nginx:pinned"}}}

    stacks = await discover_stacks(tmp_path, compose_executor=FakeComposeExecutor())

    assert len(stacks) == 1
    assert stacks[0].name == "renamed-stack"
    assert stacks[0].compose_config["services"]["web"]["image"] == "nginx:pinned"


@pytest.mark.asyncio
async def test_raw_compose_text_preserves_unsubstituted_env_vars(tmp_path: Path) -> None:
    """RAW_COMPOSE_CONFIG_SPEC.md: raw_compose_text must never resolve
    `.env` substitution, even when a real ComposeExecutor (which DOES
    resolve it, for compose_config) is supplied — this is the whole point
    of the field: a `${VAR}` referencing a secret must stay literal, never
    the real value, since this is what sensor.py displays in HA."""
    (tmp_path / "stack_1").mkdir()
    raw_text = "services:\n  app:\n    environment:\n      SESSION_SECRET: ${SESSION_SECRET}\n"
    (tmp_path / "stack_1" / "docker-compose.yml").write_text(raw_text)
    (tmp_path / "stack_1" / ".env").write_text("SESSION_SECRET=super-secret-value\n")

    class FakeComposeExecutor:
        async def get_compose_config(self, stack_dir: Path) -> dict:
            # Simulates `docker compose config` actually resolving .env —
            # this resolved value must NOT leak into raw_compose_text.
            return {
                "services": {
                    "app": {"environment": {"SESSION_SECRET": "super-secret-value"}}
                }
            }

    stacks = await discover_stacks(tmp_path, compose_executor=FakeComposeExecutor())

    assert len(stacks) == 1
    assert stacks[0].raw_compose_text == raw_text
    assert "${SESSION_SECRET}" in stacks[0].raw_compose_text
    assert "super-secret-value" not in stacks[0].raw_compose_text
    # compose_config (the resolved internal form, used for real logic like
    # protection labels/image refs) is unaffected by this change.
    assert stacks[0].compose_config["services"]["app"]["environment"]["SESSION_SECRET"] == (
        "super-secret-value"
    )


@pytest.mark.asyncio
async def test_raw_compose_text_empty_when_no_compose_executor(tmp_path: Path) -> None:
    (tmp_path / "stack_1").mkdir()
    raw_text = "services:\n  web:\n    image: nginx:latest\n"
    (tmp_path / "stack_1" / "docker-compose.yml").write_text(raw_text)

    stacks = await discover_stacks(tmp_path)

    assert stacks[0].raw_compose_text == raw_text
