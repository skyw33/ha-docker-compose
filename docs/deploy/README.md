# Deploying to HA Container (Docker)

The Home Assistant image itself stays completely stock
(`ghcr.io/home-assistant/home-assistant:stable`, unmodified) — this
integration never runs `docker compose` as a local subprocess of the HA
process, and never mounts the raw Docker socket into HA's own container.

Three pieces:
1. **`docker-socket-proxy`** — the only component with direct (read-only)
   access to `/var/run/docker.sock`. Exposes a filtered subset of the
   Docker Engine API over plain TCP.
2. **A sidecar container** (`docker_cli_sidecar`) — has the real
   `docker`/`docker compose` binaries, does nothing but sit idle, and
   talks to Docker *through the proxy* (`DOCKER_HOST=tcp://docker-socket-proxy:2375`),
   never via a direct socket mount. This integration Docker-execs into it
   to run compose commands.
3. **`homeassistant`** — talks to the proxy too, but since it runs with
   `network_mode: host` (required for HA's own mDNS/discovery features)
   it can't reach the proxy via the internal Docker network's service-name
   DNS the way the sidecar can. It reaches the proxy via the host's own
   loopback address instead — `tcp://127.0.0.1:2375`, since a container on
   host networking shares the host's localhost.

These live in **two separate stacks**, each its own folder under your
stacks root — see `example-stacks/docker-infra/docker-compose.yml` and
`example-stacks/homeassistant/docker-compose.yml`. They're split
deliberately: `docker-infra` (the proxy + sidecar) is genuinely
self-referential — the sidecar is what executes every compose command
this integration issues, including any command that would target its own
container — so both its services carry
`labels: ["ha_docker_compose.protection=full"]`, which tells the
integration never to create a switch or restart/pull button for them.
`homeassistant` gets no such label and is a
fully ordinary, unprotected stack — full switch, full restart, full pull
button, same as any other stack you manage with this integration.

## Why a proxy at all

Mounting `/var/run/docker.sock` directly into any container gives that
container root-equivalent access to the whole host — it can start a new
container with the host filesystem bind-mounted in and escape. Routing
through `docker-socket-proxy` instead means only the proxy ever touches
the real socket (read-only), and it only forwards the specific API endpoint
groups it's configured to allow (see below) — a compromised HA or sidecar
container can't do anything the proxy wasn't explicitly told to permit.

**The proxy's port binding is the one line in this setup where a typo has
real security consequences** — double-check it after any compose-file
edit. For HA on the *same* host (the default, `network_mode: host`),
bind to loopback only: `"127.0.0.1:2375:2375"`, never a bare
`"2375:2375"`. For HA on a *separate* host (multiple sites, one config
entry per Docker host — see the main README), loopback can't work, so
bind to this machine's own LAN address instead and firewall it to the HA
host specifically, not the whole network — see
`example-stacks/docker-infra/docker-compose.yml`'s `ports:` comments for
both cases, and the main README's "Using docker-socket-proxy" section for
why: with `CONTAINERS`, `EXEC` and `POST` all enabled (required either
way), anything that can reach this port can run commands in your
containers — effectively control of the host.

## Proxy permissions

`docker-socket-proxy` is allow-list-only: every endpoint group defaults to
disabled unless its environment variable is set to `1`. The full list of
variables this integration and `docker compose` itself need — and why —
lives in one place, the main README's ["Using
docker-socket-proxy"](../../README.md#using-docker-socket-proxy) section;
this file doesn't repeat it, to avoid the two drifting out of sync.
`example-stacks/docker-infra/docker-compose.yml` already sets every
variable from that table, so if you're using the example as-is there's
nothing further to configure here.

## What you still need (unchanged from the sidecar-exec spec)

**The stacks root mounted at the identical absolute path** in the sidecar
and on the host — e.g. host `/opt/stacks` → sidecar `/opt/stacks`, never
`/opt/stacks` → `/data/stacks`. The sidecar's `docker compose` process
resolves any relative bind-mount paths declared inside a *managed* stack's
own `docker-compose.yml` client-side, before sending them to the daemon —
a path mismatch here means those bind mounts land in the wrong place. Do
not add path-remapping logic to work around this; fix the mount instead.

## Steps

1. Copy `custom_components/ha_docker_compose` into your HA config folder's
   `custom_components/` (the same host folder you bind-mount to `/config`).
2. Under your stacks root, create `docker-infra/` and `homeassistant/`
   folders and copy in the matching files from `example-stacks/`, filling
   in your real paths. Bring `docker-infra` up first
   (`docker compose up -d` from that folder), then `homeassistant`.
3. Confirm the proxy is up and NOT reachable from elsewhere: from another
   machine on your LAN, `curl http://<synology-ip>:2375/version` should
   fail to connect. From the Docker host itself, `curl http://127.0.0.1:2375/version`
   should succeed.
4. In HA: Settings → Devices & Services → Add Integration → "Docker
   Compose Manager". Fill in:
   - Stacks root: the identical path from step 2 (e.g. `/opt/stacks`)
   - Docker host address: `tcp://127.0.0.1:2375`
   - Sidecar container name: `docker_cli_sidecar`
5. Check Settings → System → Logs after adding it — discovery logs how
   many stacks it found (`__init__.py`'s `_LOGGER.info` line). You should
   see both `docker-infra` and `homeassistant` as discovered stacks;
   `docker-infra` will have no switch or pull-update button (by design —
   its services are labeled `protection=full`, see above), `homeassistant`
   will have the full set.

## Adding a second host (remote site)

One config entry per Docker host (see the main README's "One Docker
daemon per config entry" limitation) — repeat this for a NAS or any other
machine you want managed as its own site, alongside the steps above.

**On the remote host**: deploy `docker-infra` as in step 2 above, with
two differences:

- Publish the proxy port on that machine's own LAN address instead of
  loopback (e.g. `"192.168.1.50:2375:2375"`, not `"127.0.0.1:2375:2375"`
  — see the example's own `ports:` comments), and restrict it to the
  Home Assistant host's address specifically, not the whole LAN. Note
  that `ufw` does not filter Docker-published ports at all; use a rule
  in the `DOCKER-USER` iptables chain, or your router/firewall, instead.
- The stacks root must be reachable at the identical absolute path from
  **Home Assistant's own filesystem too**, not only the sidecar's — this
  integration reads compose files directly to discover stacks, before
  the sidecar is ever involved (see `discovery.py`). On one host that's
  automatic; across two, it means the stacks root needs to be shared
  over the network (NFS/SMB or similar) and mounted at the same absolute
  path on both machines. A folder that only exists locally on the remote
  host won't be visible to HA's own discovery, regardless of what the
  sidecar can see.

**In Home Assistant**: Settings → Devices & Services → Add Integration →
"Docker Compose Manager" again. The setup form's fields (exact labels):

- **"Docker host address"**: `tcp://<remote-ip>:2375`
- **"Stacks root folder"**: the identical path from above, as seen from
  Home Assistant's own filesystem (which per the previous point must
  match what the remote sidecar sees too)
- **"Sidecar container name"**: the remote sidecar's container name —
  reusing the same name as your first entry's sidecar is fine, since
  each config entry talks to its own Docker host independently
- **"Site name"**: a name for this host, e.g. `nas`. Optional — left
  blank, one is derived from the stacks root folder's own name instead.

The site name appears in two places: in that host's stack and service
device names (e.g. `jellyfin (nas)`), and in the two per-site total
sensors' names and entity IDs (`Total Container CPU nas` →
`sensor.total_container_cpu_nas`). See the main README's "Using
docker-socket-proxy" section for the full proxy-variable table and the
security note — not repeated here.

**Verify**:

- `curl http://<remote-ip>:2375/version` succeeds from the Home Assistant
  host, and fails to connect from any other machine.
- The new entry's stacks show up (Settings → System → Logs has the same
  discovery line as step 5 above, for this entry).
- Settings → Devices & Services → this entry → **Reload** only re-scans
  this entry's own compose files — the first host's stacks are
  untouched.

**If the second site doesn't come up**:

- **Port unreachable from the HA host**: re-check the bind address and
  firewall rule above; confirm with `curl` from the HA host itself
  before touching HA's own config.
- **Firewall looks right but still reachable from the LAN**: if you used
  `ufw`, it doesn't see Docker-published ports — check `DOCKER-USER` or
  your router instead.
- **"No stacks found"**: almost always the stacks-root path existing on
  one side (HA or the sidecar) but not the other, or at a different
  absolute path — confirm both independently, not just one.
- **`INFO` blocked on the remote proxy**: doesn't stop the entry from
  coming up — the per-site CPU total sensor just falls back to
  `online_cpus` (see the main README's `INFO=1` row).

## If something isn't reachable

- **Compose actions fail, error names the sidecar container**: check its
  name matches your config exactly and that it's running (`docker ps`).
- **Stats/state sensors never populate, or the config entry fails to set
  up**: check the proxy container is running and that `homeassistant` can
  actually reach `127.0.0.1:2375` (test with `curl` from inside the
  `homeassistant` container if you can exec into it).
- **A specific action fails with a 403-flavored error surfaced from
  Docker**: the proxy is rejecting that endpoint group — see "Proxy
  permissions" above.
