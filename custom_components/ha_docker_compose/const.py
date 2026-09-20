"""Constants for the HA Docker Compose Manager integration."""

DOMAIN = "ha_docker_compose"

CONF_STACKS_ROOT = "stacks_root"
CONF_DOCKER_HOST = "docker_host"
CONF_SIDECAR_CONTAINER = "sidecar_container"
CONF_POLL_INTERVAL = "poll_interval"
# Stored in entry.options, never entry.data — see MULTI_SITE_IDENTITY_SPEC.md.
# Lets a reconfigure step change just this one field via
# async_update_reload_and_abort(options=...) without touching the rest of
# the entry's data at all.
CONF_SITE_NAME = "site_name"

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

# One distinct, stable `kind` value per entity class, set via
# stack_attributes()/service_attributes()/the site totals' own attribute —
# see MULTI_SITE_IDENTITY_SPEC.md's dashboard-contract amendment. Replaces
# icon-based discrimination in a dashboard (a user-customized icon would
# otherwise break it): a dashboard resolves a specific entity on a device
# by (device_id, kind) instead of (domain, icon). Never derived from or
# equal to any icon/name string — a value here changing is a deliberate,
# reviewed rename, not an incidental side effect of a display tweak.
KIND_STACK_STATE = "stack_state"
KIND_STACK_PULL_BUTTON = "stack_pull_button"
KIND_STACK_CHECK_BUTTON = "stack_check_button"
KIND_STACK_SWITCH = "stack_switch"
KIND_STACK_UPDATE_AVAILABLE = "stack_update_available"
KIND_STACK_COMPOSE_CONFIG = "stack_compose_config"
KIND_SERVICE_STATE = "service_state"
KIND_SERVICE_CPU = "service_cpu"
KIND_SERVICE_MEMORY = "service_memory"
KIND_SERVICE_UPTIME = "service_uptime"
KIND_SERVICE_LOG_COMMAND = "service_log_command"
KIND_SERVICE_LAST_FETCHED_LOGS = "service_last_fetched_logs"
KIND_SERVICE_FETCH_LOGS_BUTTON = "service_fetch_logs_button"
KIND_SERVICE_RESTART_BUTTON = "service_restart_button"
KIND_SERVICE_RUNNING_SWITCH = "service_running_switch"
KIND_SERVICE_UPDATE_AVAILABLE = "service_update_available"
KIND_SERVICE_RUNNING_DIGEST = "service_running_digest"
KIND_SERVICE_LAST_PULLED = "service_last_pulled"
KIND_SERVICE_LATEST_REGISTRY_TAG = "service_latest_registry_tag"
KIND_SERVICE_PULL_TARGET_VERSION = "service_pull_target_version"
KIND_SERVICE_LATEST_GITHUB_RELEASE = "service_latest_github_release"
KIND_SERVICE_DETECTED_VERSION = "service_detected_version"
KIND_SITE_TOTAL_CPU = "site_total_cpu"
KIND_SITE_TOTAL_MEMORY = "site_total_memory"
