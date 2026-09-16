"""Constants for the HA Docker Compose Manager integration."""

DOMAIN = "ha_docker_compose"

CONF_STACKS_ROOT = "stacks_root"
CONF_DOCKER_HOST = "docker_host"
CONF_SIDECAR_CONTAINER = "sidecar_container"
CONF_POLL_INTERVAL = "poll_interval"

# A full Docker daemon address, per aiodocker's `url` parameter — either
# `unix:///path/to/docker.sock` or `tcp://host:port`. Defaults to the
# docker-socket-proxy's localhost-only TCP endpoint (see
# SOCKET_PROXY_SPEC.md); nothing in this integration mounts the raw
# /var/run/docker.sock directly anymore.
DEFAULT_DOCKER_HOST = "tcp://127.0.0.1:2375"
# Compose commands run via Docker exec into this container, not as a local
# subprocess of the HA process — see SIDECAR_EXEC_SPEC.md.
DEFAULT_SIDECAR_CONTAINER = "docker_cli_sidecar"

# Fast stats/state poll cadence (StacksCoordinator) — configurable per
# config entry (per Docker host/node), not per stack. Single source of
# truth: coordinator.py's FAST_POLL_INTERVAL timedelta is derived from
# this, and config_flow.py's schema default is this same value, so the
# "out of the box" default only needs to change in one place.
DEFAULT_POLL_INTERVAL_SECONDS = 15
# Below this, polling risks hammering the Engine API/proxy for no real
# benefit — enforced as a hard minimum in config_flow.py's schema.
MIN_POLL_INTERVAL_SECONDS = 5
# Not enforced as a hard maximum — a deliberately long interval is a valid
# choice (e.g. a remote node over a slow link). Above this, config_flow.py
# only logs a one-time warning, since stale stats become less useful as a
# "live" dashboard well past this point, but there's no reason to block it.
HIGH_POLL_INTERVAL_WARNING_THRESHOLD_SECONDS = 300

COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

STACK_STATE_RUNNING = "running"
STACK_STATE_PARTIAL = "partial"
STACK_STATE_STOPPED = "stopped"
STACK_STATE_ORPHANED = "orphaned"
# Transient, in-memory only (see StacksCoordinator.pulling_stacks) — never
# computed by _derive_stack_state, never persisted. Overrides whatever the
# real container-derived state would be, for exactly the duration of an
# in-progress Pull update.
STACK_STATE_UPDATING = "updating"

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# button.{stack}_{service}_fetch_logs — a fixed, non-configurable tail
# count for v1 (matches HA-Docker-Manager's own precedent). See
# LOGS_BUTTON_SPEC.md — deliberately not exposed as a config-flow option
# unless it turns out to matter in practice.
LOG_FETCH_TAIL_LINES = 100
