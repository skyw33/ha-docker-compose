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

This integration keeps Docker tooling and Docker socket access outside the
Home Assistant container. Home Assistant does not need the Docker CLI
installed, and it does not need `/var/run/docker.sock` mounted directly
into the stock `ghcr.io/home-assistant/home-assistant` container.

Instead, the integration relies on two small helper containers:

- **A Docker CLI sidecar** that provides the real `docker` and
  `docker compose` commands. The integration runs Compose commands inside
  this sidecar whenever it needs to inspect or manage a stack. The sidecar
  must mount your stacks directory at the *same absolute path* that exists
  on the Docker host. For example, if your stacks live under
  `/volume1/docker` on the host, they should also appear under
  `/volume1/docker` inside the sidecar rather than being remapped to a
  different path such as `/stacks`. This keeps Compose file paths and
  absolute bind-mount paths consistent.

- **A `docker-socket-proxy` container** that is the only component with a
  direct mount of `/var/run/docker.sock`. It acts as a controlled gateway
  to the Docker Engine API and exposes only the API areas enabled in its
  configuration. Home Assistant and the Docker CLI sidecar communicate
  with Docker through this proxy over TCP instead of mounting the Docker
  socket themselves.

For a Docker-based Home Assistant installation, the supporting sidecar and
proxy containers are deployed alongside Home Assistant and your existing
Compose stacks. The same overall architecture can also be used with Home
Assistant OS or Supervised, but the sidecar and proxy still need to be run
separately because Home Assistant does not manage arbitrary Docker Compose
stacks on your behalf.

### Using docker-socket-proxy

[`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)
exposes only the Docker Engine API areas you turn on, each behind its own
environment variable — most sections are off by default (`EVENTS`,
`PING` and `VERSION` are the exceptions, on by default). This table
covers only what *this integration's own Python code* calls directly
against the Engine API — not what `docker compose` itself needs once
it's running inside the sidecar (a separate, broader surface: image
pulls, container create/start/stop, networks, volumes, and so on —
outside this integration's own code and not audited here).

| Variable | Required? | What it gates here |
|---|---|---|
| `CONTAINERS=1` | Required | Live container list/inspect/stats — state, CPU and memory sensors — and container logs (the Fetch Logs button). Also needed for creating the exec sessions compose commands run through (`POST /containers/{id}/exec`). |
| `IMAGES=1` | Required | Local image inspection for digest/label comparison (`update_available`, and the GitHub-release OCI-label fallback). |
| `EXEC=1` | Required | Starting and polling the exec sessions used to run `docker compose` (pull, up, down, restart, start, stop, config) inside the sidecar. |
| `POST=1` | Required | docker-socket-proxy denies every non-`GET`/`HEAD` request unless this is set. Both creating an exec session (`POST /containers/{id}/exec`) and starting it (`POST /exec/{id}/start`) are `POST` requests; everything else in this table is a plain `GET`. |
| `INFO=1` | Optional | Host CPU core count (`GET /info`), used to normalize the per-site "Total Container CPU" sensor to a 0–100% figure. Without it, that sensor falls back to the `online_cpus` value already present in running containers' stats. If nothing is running, it reports `0`. It only goes unknown if something is running and its stats report no core count either. |

> **Security note:** with `CONTAINERS`, `EXEC` and `POST` all enabled — required for this integration to manage your stacks at all — anything that can reach the proxy's published port can run arbitrary commands inside your containers, which is effectively control of the host. Bind the published port to a specific interface or firewall it to the Home Assistant host only; never expose it beyond a trusted network.

The example [`docker-infra`](docs/deploy/example-stacks/docker-infra/docker-compose.yml)
compose file enables everything the integration and the sidecar need. If
you write your own proxy configuration, also enable `NETWORKS` and
`VOLUMES`, which `docker compose` uses to create a stack's networks and
volumes.

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
interval above at once *except* the GitHub release lookup — it forces an
immediate digest check *and* tag walk for that stack, regardless of
staleness, at the cost of the full per-tag registry traffic the scheduled
sweep normally avoids, but doesn't touch `latest_github_release`, which
isn't wired to this button.

**The GitHub release lookup uses GitHub's unauthenticated API** (60
requests/hour per IP, shared across every stack and service on that
Docker host). It's enabled by default and checks every service with a
resolvable `org.opencontainers.image.source` label independently each
12-hour cycle — two services pointing at the same repo cost two requests,
not one; there's no de-duplication across services sharing a repo. A
rate-limited (or otherwise failing) check simply returns nothing for that
service, logged at debug level, with no retry sooner than the next
scheduled cycle — no backoff, and no manual retry button, since Check for
Update (Registry) doesn't reach this lookup either. Nothing here is
persisted across a restart: the check reruns from scratch on every HA
start, so a value that was showing before a restart is gone (not just
stale) until the next check succeeds.

## Example dashboard

[`docs/dashboard-examples/`](docs/dashboard-examples/) has an example
Lovelace dashboard: a stack overview grid grouped by site, plus a detail
panel for whichever stack is selected. It needs `custom:button-card`
(the only HACS frontend card it uses) and two helper entities — see that
directory's own README for the exact helper definitions, usage, and a
screenshot.

Every stack- and service-level entity already exposes plain `site`/
`stack`/`service`/`kind` attributes specifically so a dashboard like this
never needs to parse `entity_id` strings.

![Dashboard screenshot](docs/dashboard-examples/dashboard.png)


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
