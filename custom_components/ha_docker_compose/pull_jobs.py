"""Persisted, restart-resumable pull+recreate jobs.

See DETACHED_EXEC_SPEC.md for why this exists: `docker compose up -d` can
recreate the very container HA itself runs in, and an *attached* exec
watching that command shares its fate with the connection watching it —
which is HA's own process. If HA dies mid-command (guaranteed when the
stack being recreated is the one hosting HA; possible for any stack on an
unrelated restart/crash/OOM), the attached approach loses track of the
command with no exception ever raised, no record of what happened, and no
digest-history write.

PullJobRunner instead starts each step (`pull`, then `up -d`) as a
DETACHED exec (engine.py / compose.py's start_detached) and polls for
completion — no connection needs to survive the command's full duration.
The in-flight step is persisted (PullJobStore, Store-backed like
storage.py's digest history) before polling begins, so a fresh HA process
can find it on the next startup and resume exactly where the killed
process left off, rather than losing track of it.

digest-history writing and the update-check auto-refresh
(AUTO_REFRESH_AFTER_PULL_SPEC) are unchanged in what they do — only how
they're triggered: "poll detected completion" instead of "stream reached
EOF", and possibly from a resumed job on a different HA process than the
one that started it.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .compose import ComposeCommandError, ComposeExecutor
from .const import DOMAIN
from .coordinator import StacksCoordinator
from .engine import DockerEngineClient, ExecNotFoundError, SidecarNotAvailableError
from .image_ref import tag as image_tag
from .storage import DigestHistoryStore
from .tag_walk_coordinator import TagWalkCoordinator
from .update_coordinator import UpdateCheckCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

STEP_PULL = "pull"
STEP_UP = "up"

POLL_INTERVAL = 2.0
# Generous but bounded — a stuck/hung command shouldn't be polled forever.
# Exceeding this doesn't kill the command (it may still finish in the
# sidecar); it just stops this process watching it and says so clearly.
MAX_WAIT_SECONDS = 1800.0


class PullJobStore:
    """One instance per config entry, keyed by stack name — only one
    in-flight pull job per stack is tracked at a time; a fresh press
    naturally supersedes any prior record for that stack."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_pull_jobs")
        self._data: dict[str, dict[str, Any]] | None = None

    async def async_load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            self._data = await self._store.async_load() or {}
        return self._data

    async def async_set(self, stack_name: str, step: str, exec_id: str, log_path: str) -> None:
        data = await self.async_load()
        data[stack_name] = {"step": step, "exec_id": exec_id, "log_path": log_path}
        await self._store.async_save(data)

    async def async_clear(self, stack_name: str) -> None:
        data = await self.async_load()
        if data.pop(stack_name, None) is not None:
            await self._store.async_save(data)


class PullJobRunner:
    """Starts and (resumably) drives pull+recreate jobs to completion."""

    def __init__(
        self,
        hass: HomeAssistant,
        engine: DockerEngineClient,
        compose_executor: ComposeExecutor,
        coordinator: StacksCoordinator,
        update_coordinator: UpdateCheckCoordinator,
        tag_walk_coordinator: TagWalkCoordinator,
        digest_history: DigestHistoryStore,
        job_store: PullJobStore,
    ) -> None:
        self._hass = hass
        self._engine = engine
        self._compose = compose_executor
        self._coordinator = coordinator
        self._update_coordinator = update_coordinator
        self._tag_walk_coordinator = tag_walk_coordinator
        self._digest_history = digest_history
        self._job_store = job_store

    async def start(self, stack_name: str, stack_dir: Path) -> None:
        """Kick off a pull+recreate job: starts the 'pull' step as a
        detached, resumable exec and returns almost immediately — the
        actual wait-for-completion (through 'pull' then 'up -d') runs as a
        background task, since it may outlive this call entirely (e.g. if
        the stack being pulled is the one hosting HA itself).

        Raises ComposeCommandError only for an immediate failure to even
        start (e.g. sidecar unreachable) — the caller (button.py) is
        expected to translate that into a HomeAssistantError as usual.
        Once the job is actually running, failures are logged, not raised,
        since by then there's no synchronous caller left to raise them to.
        """
        # Unconditional, before anything else: on a normal successful run
        # nothing else logs until _finish_success() at the very end (the
        # whole point of the poll loop is to stay quiet while waiting), so
        # without this line there is nothing here to confirm the job even
        # started until it's fully done — same blind spot button.py's own
        # entry log was added to close.
        _LOGGER.info("PullJobRunner.start() called for stack '%s'", stack_name)

        self._coordinator.pulling_stacks.add(stack_name)
        self._coordinator.async_update_listeners()

        try:
            handle = await self._compose.start_detached(stack_dir, "pull")
        except ComposeCommandError:
            self._coordinator.pulling_stacks.discard(stack_name)
            self._coordinator.async_update_listeners()
            raise

        _LOGGER.info(
            "PullJobRunner: 'pull' step started for stack '%s' as exec %s", stack_name, handle.exec_id
        )
        await self._job_store.async_set(stack_name, STEP_PULL, handle.exec_id, handle.log_path)
        self._spawn_driver(stack_name, stack_dir, STEP_PULL, handle.exec_id, handle.log_path)

    async def resume_all(self) -> None:
        """Called once at startup: find every job left in-flight by a
        process that didn't survive to finish watching it, and resume
        polling each — this is what makes a mid-pull HA restart recoverable
        instead of silently losing track of the command."""
        jobs = await self._job_store.async_load()
        if not jobs:
            return

        for stack_name, record in jobs.items():
            stack = next((s for s in self._coordinator.stacks if s.name == stack_name), None)
            if stack is None or stack.path is None:
                _LOGGER.warning(
                    "Found an in-flight pull job for stack '%s' from before an HA restart, but "
                    "the stack no longer exists on disk — dropping the stale record",
                    stack_name,
                )
                await self._job_store.async_clear(stack_name)
                continue

            _LOGGER.info(
                "Resuming in-flight pull job for stack '%s' (step '%s') found from before an "
                "HA restart",
                stack_name,
                record.get("step"),
            )
            self._coordinator.pulling_stacks.add(stack_name)
            self._spawn_driver(
                stack_name, stack.path, record["step"], record["exec_id"], record["log_path"]
            )

        self._coordinator.async_update_listeners()

    def _spawn_driver(
        self, stack_name: str, stack_dir: Path, step: str, exec_id: str, log_path: str
    ) -> None:
        self._hass.async_create_task(
            self._drive(stack_name, stack_dir, step, exec_id, log_path),
            name=f"ha_docker_compose pull job: {stack_name}",
        )

    async def _drive(
        self, stack_name: str, stack_dir: Path, step: str, exec_id: str, log_path: str
    ) -> None:
        # Unconditional: confirms the background task hass.async_create_task
        # scheduled in _spawn_driver() actually got to run at all, distinct
        # from "it's running but still polling, nothing to report yet."
        _LOGGER.info(
            "PullJobRunner._drive() running for stack '%s', step '%s', exec %s",
            stack_name,
            step,
            exec_id,
        )
        try:
            success = await self._wait_for_step(stack_name, step, exec_id, log_path)
        except ExecNotFoundError:
            _LOGGER.error(
                "Pull update for stack '%s': lost track of the in-flight '%s' command (the "
                "sidecar container itself most likely restarted, dropping its exec state) — "
                "outcome unknown, giving up on this job rather than guessing",
                stack_name,
                step,
            )
            await self._end_job(stack_name)
            return

        if not success:
            # Failure already logged in _wait_for_step with the command's
            # captured output; no update-check refresh on a failed job,
            # same condition that already gated the digest-history write
            # before this spec.
            await self._end_job(stack_name)
            return

        if step == STEP_PULL:
            try:
                handle = await self._compose.start_detached(stack_dir, "up", "-d")
            except ComposeCommandError as err:
                _LOGGER.warning(
                    "Pull update for stack '%s': starting 'up -d' after a successful pull "
                    "failed: %s",
                    stack_name,
                    err,
                )
                await self._end_job(stack_name)
                return

            await self._job_store.async_set(stack_name, STEP_UP, handle.exec_id, handle.log_path)
            await self._drive(stack_name, stack_dir, STEP_UP, handle.exec_id, handle.log_path)
            return

        # step == STEP_UP and it succeeded: the full pull+recreate is done.
        await self._finish_success(stack_name)

    async def _wait_for_step(self, stack_name: str, step: str, exec_id: str, log_path: str) -> bool:
        elapsed = 0.0
        while True:
            try:
                result = await self._compose.poll(exec_id)  # ExecNotFoundError propagates to _drive
            except SidecarNotAvailableError as err:
                # Transient — couldn't check right now, not "the command is
                # gone." Retry within the same overall MAX_WAIT_SECONDS
                # budget rather than abandoning the job over a blip.
                _LOGGER.debug(
                    "Pull update for stack '%s': transient error polling '%s' step, retrying: %s",
                    stack_name,
                    step,
                    err,
                )
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
                if elapsed >= MAX_WAIT_SECONDS:
                    _LOGGER.error(
                        "Pull update for stack '%s': could not reach the sidecar to check on "
                        "the '%s' step for %.0fs — giving up watching it",
                        stack_name,
                        step,
                        MAX_WAIT_SECONDS,
                    )
                    return False
                continue

            if not result.running:
                if result.exit_code == 0:
                    return True
                log = await self._compose.read_log(log_path)
                _LOGGER.warning(
                    "Pull update for stack '%s': '%s' step failed (exit %s): %s",
                    stack_name,
                    step,
                    result.exit_code,
                    log.strip() or "(no output captured)",
                )
                return False

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if elapsed >= MAX_WAIT_SECONDS:
                _LOGGER.error(
                    "Pull update for stack '%s': '%s' step still running after %.0fs — no "
                    "longer watching it (it may still complete in the sidecar; check manually "
                    "if this persists)",
                    stack_name,
                    step,
                    MAX_WAIT_SECONDS,
                )
                return False

    async def _finish_success(self, stack_name: str) -> None:
        stack = next((s for s in self._coordinator.stacks if s.name == stack_name), None)
        if stack is None:
            _LOGGER.warning(
                "Pull update for stack '%s' completed, but the stack is no longer known to the "
                "coordinator (folder removed?) — skipping digest-history recording",
                stack_name,
            )
        else:
            # Snapshot of tag-walk data per service, taken once before the
            # loop below (not re-read per service) purely because it's
            # already keyed by stack — see UNVERIFIED_DETECTED_VERSION_SPEC.md,
            # amended by PULL_TARGET_PREFERRED_SPEC.md: pull_target_version
            # (digest-verified — the same correlation mechanism
            # pull_target_version itself already uses, confirmed real
            # case: matter-server, 8.1.0) is preferred over
            # latest_registry_tag (a genuine assumption — pulling a
            # floating tag fetches whatever it currently points to, which
            # is *usually* the same build as the newest published tag, by
            # convention, but never proven the way a digest match would
            # be — confirmed real case: otbr, where no digest match
            # existed at all) whenever both are available for the same
            # service. Either way the result is recorded as
            # detected_version's last-resort fallback, suffixed
            # "(unverified)" uniformly regardless of source — even a
            # digest-verified match at pull time can go stale later if
            # the tag moves again before the next check, so treating both
            # as "assumed as of last pull" rather than distinguishing
            # them visually is the deliberate choice here, not a default.
            tag_walk_data = (self._tag_walk_coordinator.data or {}).get(stack_name, {})

            for service_name, service_def in (stack.compose_config.get("services") or {}).items():
                image_ref = service_def.get("image")
                if not image_ref:
                    continue
                digest = await self._engine.get_image_repo_digest(image_ref)
                if not digest:
                    _LOGGER.warning(
                        "Pull update for stack '%s', service '%s': could not read a local "
                        "RepoDigest right after pull, so nothing was recorded to the "
                        "pull-history log for it",
                        stack_name,
                        service_name,
                    )
                    continue
                tag_walk_status = tag_walk_data.get(service_name)
                assumed_version = None
                assumed_version_source = None
                if tag_walk_status:
                    if tag_walk_status.pull_target_version:
                        assumed_version = tag_walk_status.pull_target_version
                        assumed_version_source = "pull_target_version"
                    elif tag_walk_status.latest_registry_tag:
                        assumed_version = tag_walk_status.latest_registry_tag
                        assumed_version_source = "latest_registry_tag"
                await self._digest_history.async_record_pull(
                    stack_name,
                    service_name,
                    image_tag(image_ref),
                    digest,
                    assumed_version=assumed_version,
                )
                _LOGGER.info(
                    "Recorded pull history for stack '%s', service '%s': digest %s, "
                    "assumed_version=%r (source=%s)",
                    stack_name,
                    service_name,
                    digest,
                    assumed_version,
                    assumed_version_source,
                )

        await self._end_job(stack_name)

        await self._coordinator.async_request_refresh()
        await self._update_coordinator.async_request_refresh()

        _LOGGER.info("Pull update for stack '%s' completed successfully", stack_name)

    async def _end_job(self, stack_name: str) -> None:
        await self._job_store.async_clear(stack_name)
        self._coordinator.pulling_stacks.discard(stack_name)
        self._coordinator.async_update_listeners()
