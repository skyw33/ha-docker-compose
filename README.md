# HA Docker Compose Manager

A Home Assistant custom integration for managing Docker Compose stacks —
compose-first rather than container-first, a bit like a lightweight
Portainer replacement that lives inside HA itself.

**This is built for Home Assistant Container (HA running as a Docker
container) managing Docker Compose stacks on the same host** — that's
the setup this integration is designed, tested, and documented against.
HA OS/Supervised installs aren't the primary target (see
[Prerequisites](#prerequisites) for the caveats if you want to try it
anyway).

Point it at a folder of stack subdirectories (each with its own
`docker-compose.yml`/`compose.yaml`) and it discovers them, then exposes
per-stack and per-service entities for:

- **Live state and stats** — running/partial/stopped/orphaned state per
  stack, per-service container state, CPU/memory, uptime.
- **Start/stop/restart control** — backed by the real `docker compose`
  CLI (never raw container commands), so Compose's own dependency
  ordering and `stop_grace_period` handling are respected.
- **Pull + recreate**, as a detached, restart-resumable job — safe even
  when the stack being recreated is the one Home Assistant itself runs
  in.
- **Multi-signal version tracking**: digest-based update availability
  (the authoritative "should I pull" signal), a locally detected running
  version (OCI label or image tag), the newest version tag published
  anywhere on the registry, and — when a digest match can prove it —
  which published tag you'd actually land on if you pulled right now.
  Docker Hub and `ghcr.io` are supported natively (no `skopeo` or other
  external tool required).
- **Multi-host support**, via a sidecar + `docker-socket-proxy`
  architecture (see [Prerequisites](#prerequisites) below) — the Home
  Assistant container itself never needs the `docker` CLI or a mounted
  Docker socket, and you can point separate config entries at separate
  Docker hosts.


ha-docker-compose brings your container infrastructure natively into Home Assistant by organizing your workflows around logical docker-compose.yml stacks rather than a flat, disconnected list of containers. It is built on a secure, least-privilege socket-proxy and sidecar architecture that completely sandboxes your core instance while scaling to multi-host environments—allowing you to manage local boxes and remote NAS setups side by side from a single interface. Beyond basic state monitoring, it delivers true version intelligence by walking image registries and correlating digests for update tracking.


## Installation

### Manual

```sh
cp -r custom_components/ha_docker_compose <path-to-your-ha-config>/custom_components/
```

Restart Home Assistant, then go to **Settings → Devices & Services → Add
Integration** and search for **"Docker Compose Manager"**.

### HACS (custom repository)

Not yet in HACS's default repository list. To add it as a custom
repository:

1. HACS → Integrations → ⋮ (top right) → **Custom repositories**.
2. Add `https://github.com/skyw33/ha-docker-compose`, category
   **Integration**.
3. Install "Docker Compose Manager" from HACS, then restart Home
   Assistant and add it via **Settings → Devices & Services** as above.

### Setup fields

| Field | Meaning |
|---|---|
| Stacks root | Folder containing one subfolder per stack, each with its own compose file — mounted identically in HA and in the sidecar (see below). |
| Docker host address | `tcp://host:port` or `unix:///path/to/docker.sock` — defaults to a local `docker-socket-proxy` on `tcp://127.0.0.1:2375`. |
| Sidecar container name | The container this integration Docker-execs into to run `docker compose` commands. |
| Poll interval | Fast stats/state poll cadence, in seconds (default 15). |

## Prerequisites

This integration never runs `docker compose` as a local subprocess of the
Home Assistant process, and never mounts the raw Docker socket into HA's
own container — the stock `ghcr.io/home-assistant/home-assistant` image
stays completely unmodified. Instead, it relies on two small pieces of
supporting infrastructure that you deploy alongside it:

- **A sidecar container** with the real `docker`/`docker compose`
  binaries, mounting your stacks root at the *identical* absolute path as
  the host. This integration execs into it to actually run compose
  commands. The example deploy below uses the official
  [`docker:cli`](https://hub.docker.com/_/docker) image (the `cli` tag —
  just the Docker/Compose CLI binaries, no daemon), but any image with
  both binaries on `PATH` works.
- **[`docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)**
  (`tecnativa/docker-socket-proxy`), the only component with direct
  (read-only) access to `/var/run/docker.sock`, exposing a filtered,
  allow-list-only subset of the Docker Engine API over plain TCP — bound
  to localhost only. Both the sidecar and Home Assistant itself talk to
  Docker through this proxy, never via a direct socket mount.

If you're running HA in Docker yourself, see
[`docs/deploy/`](docs/deploy/) for a full worked example (two
`docker-compose.yml` files, an explanation of why the proxy matters, and
a step-by-step deploy walkthrough). If you're running HA OS/Supervised,
the same sidecar + proxy pattern still applies — you'll need to run those
two containers yourself since HA OS doesn't manage arbitrary Docker
Compose stacks itself.

## Polling

Four independent poll cycles, each on the cadence its own data actually
needs — none of them block or wait on each other:

| What | Default interval | Configurable? | Scope |
|---|---|---|---|
| Live container state/stats (CPU, memory, uptime, running state) | 15s | Yes — the "Poll interval" setup field, 5s minimum | Every discovered stack |
| Digest-based update availability (`update_available`) | 1h | No | Running stacks only |
| Registry tag-list walk (`latest_registry_tag`, `pull_target_version`) | 48h full sweep | No | Running stacks only |
| GitHub release lookup + local version detection (`latest_github_release`, `detected_version`) | 12h | No | Every discovered stack, running or not |

The registry tag-list walk is deliberately the slowest and least
frequent: fetching a project's *entire* published tag history is real,
non-trivial registry traffic — confirmed in practice at 29 paginated
requests for one image with a large tag history (2,890+ tags). Running
that on anything close to the 1-hour digest-check cadence would multiply
registry load for data that rarely changes.

To stay responsive despite that slow cadence, the digest check itself
triggers an immediate, but *stack-scoped* (not repo-wide), tag-list
walk the moment a service's `update_available` flips from off to on —
the one moment "what's the newest tag, and does it match what I'd
actually get" becomes worth knowing. The scheduled 48-hour sweep still
runs on top of that, as a backstop for the rarer case where a stack is
already outdated and its newest available tag changes again while still
outdated.

Pressing **Check for Update (Registry)** on a stack bypasses every
interval above at once — it forces an immediate digest check *and* tag
walk for that stack, regardless of staleness, at the cost of the full
per-tag registry traffic the scheduled sweep normally avoids.

## Example dashboard

[`docs/dashboard-examples/`](docs/dashboard-examples/) has an example
Lovelace dashboard: a stack overview grid plus a detail panel for
whichever stack is selected. It needs an `input_text.selected_stack`
helper entity and two HACS frontend cards
([auto-entities](https://github.com/thomasloven/lovelace-auto-entities),
[button-card](https://github.com/custom-cards/button-card)) — see that
directory's own README for the details and a screenshot.

Every per-service entity already exposes plain `stack`/`service`
attributes specifically to support a dashboard like this without any
`entity_id` string-parsing.<img width="1076" height="569" alt="dashboard" src="https://github.com/user-attachments/assets/d58ba5a9-acc5-4f03-b70c-f06f799104f6" />


## Known limitations

- **Registries**: only Docker Hub and `ghcr.io` are supported for
  digest/version checks (both speak the same anonymous-bearer-token
  Docker Registry HTTP API v2 flow). Other registries are detected and
  marked unavailable rather than silently failing, but aren't checked.
- **No semver-severity classification**: version-tracking sensors report
  "here is the newest real version tag" and "here is what your pinned
  tag currently resolves to" — never "this is a patch vs. a major
  update." Deliberately out of scope; see the failure history of tools
  that attempt this on top of Docker tag conventions.
- **No live log streaming**: log-related entities give you a
  copy-pasteable `docker logs -f` command and an on-demand last-100-lines
  snapshot, not a live tail inside HA.
- **One Docker daemon per config entry**: add multiple config entries for
  multiple hosts; a single entry doesn't span hosts.
- Private registries and registries requiring non-anonymous auth aren't
  supported for the update-tracking features (stack management itself is
  unaffected — it just means "update available"/"newest tag" data won't
  populate for those images).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
PYTHONPATH=custom_components .venv/bin/pytest -q
```

Pure-logic modules (discovery, compose-CLI wrapper, registry tag
filtering, version detection, etc.) are unit tested without a running
Home Assistant instance — `tests/conftest.py` stubs the top-level package
so these tests can import submodules without pulling in the real
`homeassistant` package. Modules that directly depend on Home Assistant's
own runtime (coordinators, config flow, entity platforms) aren't unit
tested in this setup; changes there are verified by hand against the
integration's actual behavior.

```
custom_components/ha_docker_compose/
  manifest.json         HACS/HA metadata, requirements (aiodocker, packaging)
  const.py              Domain, config keys, defaults
  discovery.py           Filesystem scan: what stacks exist under stacks_root
  compose.py              ComposeExecutor: compose commands, run via sidecar exec
  engine.py                 aiodocker wrapper: live container state + stats, Docker exec into the sidecar
  coordinator.py             Fast-poll coordinator joining engine state to discovered stacks
  device_ids.py                Pure device-identifier logic — what devices should exist, for reload pruning
  entity.py                     Per-stack/per-service device grouping; shared stack/service attribute helper
  image_ref.py                   Docker image reference parsing
  registry_client.py               Native Docker Registry HTTP API v2 client (Docker Hub + ghcr.io)
  tag_filter.py                     Registry tag-list filtering + version-aware newest-tag selection
  storage.py                         Persisted digest-pull history (survives restarts)
  update_coordinator.py               Digest comparison (fast cadence)
  tag_walk_coordinator.py               Registry tag-list walk (slow cadence + scoped event-triggered refresh)
  github_release.py                       OCI source-label parsing + GitHub releases/latest fetch
  oci_labels.py                             Shared image/container label resolution
  version_detect.py                          Local version detection: OCI label or image tag
  github_coordinator.py                        Release lookup + local version detection
  protection.py                                 protection=full compose label parsing
  pull_jobs.py                                   Detached, restart-resumable pull+recreate jobs
  config_flow.py                                  Setup UI
  sensor.py                                        State/stats/version/release entities
  switch.py                                         Stack/service start-stop
  button.py                                          Pull, check-updates, restart, fetch-logs
  binary_sensor.py                                    update_available per service
```

## License

[MIT](LICENSE)
