"""Filesystem-based stack discovery.

Compose files on disk are the source of truth. This module answers exactly
one question: "what stacks exist under <stacks_root>?" It does not touch the
Docker Engine API at all — joining discovered stacks against currently
running containers (for state derivation: running/partial/stopped/orphaned)
is the coordinator's job, not discovery's.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compose import ComposeExecutor, read_raw_compose_file
from .const import COMPOSE_FILENAMES

_LOGGER = logging.getLogger(__name__)


@dataclass
class StackInfo:
    """A single discovered stack.

    `path` is None only for a synthetic StackInfo representing an orphaned
    project (containers running with a compose project label but no
    matching folder on disk) — see coordinator.py.
    """

    name: str
    path: Path | None
    compose_filename: str
    has_env_file: bool
    compose_config: dict[str, Any] = field(default_factory=dict)
    # The compose file's exact on-disk text, unparsed and un-substituted —
    # `${VAR}` stays literal, never resolved against `.env`. Deliberately
    # separate from compose_config above (which IS `.env`-resolved, via
    # `docker compose config`, and stays that way — internal logic like
    # protection-label parsing and image-ref extraction genuinely needs
    # the resolved form). This field exists only for
    # sensor.{stack}_compose_config's display — see
    # RAW_COMPOSE_CONFIG_SPEC.md: the resolved form was removed from
    # display entirely because it's the one place a real secret value
    # (e.g. SESSION_SECRET from `.env`) could end up visible in HA.
    raw_compose_text: str = ""

    @property
    def service_names(self) -> list[str]:
        return list(self.compose_config.get("services", {}) or {})


def _find_compose_file(stack_dir: Path) -> Path | None:
    for filename in COMPOSE_FILENAMES:
        candidate = stack_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _read_raw_compose_text(compose_file: Path) -> str:
    """Exact on-disk text, no YAML parsing and no `.env` substitution —
    see StackInfo.raw_compose_text."""
    try:
        return compose_file.read_text(encoding="utf-8")
    except OSError:
        _LOGGER.exception("Failed to read raw compose file text for %s", compose_file)
        return ""


async def discover_stacks(
    stacks_root: Path, compose_executor: ComposeExecutor | None = None
) -> list[StackInfo]:
    """Scan stacks_root one level deep and return every discovered stack.

    Each immediate subfolder containing a docker-compose.yml/compose.yaml is
    a stack. Stack name defaults to the folder name, unless the resolved
    compose config defines a top-level `name:`, which takes precedence
    (matches Compose's own resolution order).

    compose_executor is optional: config-flow validation calls this before
    a ComposeExecutor exists (no engine connection needed just to confirm
    "are there stacks here"), so a raw (un-substituted) YAML read is used
    instead in that case. Real startup discovery (__init__.py) always
    passes one, so `.env` substitution is correct by construction there.
    """
    if not stacks_root.is_dir():
        raise FileNotFoundError(f"stacks_root does not exist or is not a directory: {stacks_root}")

    stacks: list[StackInfo] = []

    for entry in sorted(stacks_root.iterdir()):
        if not entry.is_dir():
            continue

        compose_file = _find_compose_file(entry)
        if compose_file is None:
            continue

        has_env_file = (entry / ".env").is_file()

        try:
            if compose_executor is not None:
                compose_config = await compose_executor.get_compose_config(entry)
            else:
                compose_config = read_raw_compose_file(entry)
        except Exception:  # noqa: BLE001 - one bad stack must not abort discovery
            _LOGGER.exception("Failed to resolve compose config for %s", entry)
            compose_config = {}

        stack_name = compose_config.get("name") or entry.name

        stacks.append(
            StackInfo(
                name=stack_name,
                path=entry,
                compose_filename=compose_file.name,
                has_env_file=has_env_file,
                compose_config=compose_config,
                raw_compose_text=_read_raw_compose_text(compose_file),
            )
        )

    return stacks
