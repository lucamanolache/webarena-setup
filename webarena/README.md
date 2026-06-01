> :warning: **This is not an official WebArena repo. For the official instructions refer to [WebArena](https://github.com/web-arena-x/webarena/tree/main/environment_docker)**

# webarena-setup

Setup scripts and hot-swap reset server for WebArena.

## Prerequisites

- Debian 12 server
- Required archive files (see [Get the files](#get-the-files))

Install dependencies:
```bash
sudo bash 00_install_deps.sh
```

## Get the files

Download the docker images from the [official webarena repo](https://github.com/web-arena-x/webarena/tree/main/environment_docker):
- `shopping_final_0712.tar`
- `shopping_admin_final_0719.tar`
- `postmill-populated-exposed-withimg.tar`
- `gitlab-populated-final-port8023.tar`
- `wikipedia_en_all_maxi_2022-05.zim`

Download the OpenStreetMap files from Zenodo:
```sh
wget https://zenodo.org/records/12636845/files/openstreetmap-website-db.tar.gz
wget https://zenodo.org/records/12636845/files/openstreetmap-website-web.tar.gz
wget https://zenodo.org/records/12636845/files/openstreetmap-website.tar.gz
```

## Configure

Edit `00_vars.sh` with your hostname/IP and ports. Set `ARCHIVES_LOCATION` to where you placed the downloaded files.

## Quick start

First-time setup + run everything:
```bash
sudo bash run_all.sh --setup
```

Subsequent runs (`:ready` images already exist):
```bash
sudo bash run_all.sh
```

This starts the homepage server and the reset server, which manages all containers.

## Step-by-step setup

If you prefer to run things individually:

```bash
# 1. Load images into podman
sudo bash 01_docker_load_images.sh

# 2. Create, start, and patch containers
sudo bash 02_docker_remove_containers.sh
sudo bash 03_docker_create_containers.sh
sudo bash 04_docker_start_containers.sh
sudo bash 05_docker_patch_containers.sh

# 3. Commit patched containers as :ready images (used by the pool)
sudo bash 08_checkpoint.sh

# 4. Start homepage server (port 80)
sudo bash 06_serve_homepage.sh &

# 5. Start reset server (manages all containers)
sudo bash 07_serve_reset.sh
```

The reset server (`07_serve_reset.sh`) runs `server.py --port 7565 --init` which:
1. Starts static services (OpenStreetMap, Wikipedia)
2. Boots one **active** container per service (standbys warm later, on demand)
3. Writes nginx config to route public ports to active instances
4. Starts a background warmer that warms standbys for used services and shrinks idle ones
5. Starts the HTTP API on port 7565

On Ctrl+C or SIGTERM, the server tears down all containers and cleans up nginx.

## Architecture

### Container pool

Each service maintains a pool of container instances. One is **active** (serving traffic), the rest are **ready** (standby) or **rebuilding**.

| Service | Public port | Warm target | Min (idle) | Max | Boot time |
|---------|------------|-------------|-----------|-----|-----------|
| shopping | 8082 | 2 | 1 | 2 | ~2 min |
| shopping_admin | 8083 | 2 | 1 | 2 | ~2 min |
| forum | 8080 | 2 | 1 | 2 | ~1 min |
| gitlab | 9001 | 5 | 1 | 6 | ~4 min |
| wikipedia | 8081 | — (static) | — | — | ~10 sec |
| openstreetmap | 443 | — (static) | — | — | ~30 sec |

Each service has a reserved internal host-port range. Instances prefer `port_base + index`, and if that port is busy the server picks the next free port inside that service's reserved range and stores it in `pool_state.json`. For example, shopping uses the `18280+` range and gitlab uses the `19001+` range.

### Usage-based sizing (don't waste resources)

The pool size adapts to usage so idle arenas don't burn resources:

- **`--init` boots only the active container per service**, so the stack starts serving quickly instead of waiting for every standby to boot.
- A service warms standbys up to its **warm target** only *after it is reset* (i.e. actually used).
- A service with no reset for `WEBARENA_IDLE_TIMEOUT` seconds (default 30 min) is considered **idle** and is shrunk back to **min** (just the active container).
- A background **warmer** thread reconciles each pool toward its desired size every `WEBARENA_WARMER_INTERVAL` seconds (default 15s): it warms standbys for used services, shrinks idle ones, retries failed instances, and self-heals a dead active container.

Both knobs are environment variables read at startup.

### Limited concurrent boots (robustness)

Launching many containers concurrently is the main source of flaky podman
failures. Every container **boot** (`podman create` + `start`) goes through a
single process-wide semaphore, so no matter how many resets arrive at once, only
a bounded number of containers are created/started at a time. By default that
bound is **1** (one podman boot at a time); raise it with `--max-concurrent-boots N`
for faster warmup at the cost of more load on podman. Health checks run *outside*
the semaphore, so multiple services still warm concurrently — the bound only
applies to the create+start burst. `podman create`/`start` also retry transient
failures (e.g. "database is locked") with backoff.

### Reset flow (~0.05s)

1. Mark the targeted service(s) as used (so the warmer keeps them warm).
2. Find next ready standby (round-robin).
3. Update nginx config to point the public port at the new instance and reload.
4. Mark old instance as rebuilding; rebuild it in the background (serialized via the boot lock).

If a target has no ready standby (e.g. its first reset after being idle), the reset returns `503` and the warmer brings a standby up for next time.

### Static services

OpenStreetMap (db + web) and Wikipedia are started once and never reset. They are managed by the server (started on init, stopped on teardown) but excluded from the pool/reset cycle.

## API

### Reset all services

```
GET http://localhost:7565/reset
```

### Reset specific services

```
GET http://localhost:7565/reset?services=shopping,gitlab
```

### Check status

```
GET http://localhost:7565/status
```

Returns:
```json
{
  "status": "serving",
  "services": {
    "shopping": {"active": 0, "active_up": true, "ready_count": 1, "managed": 2, "idle": false},
    "gitlab":   {"active": 2, "active_up": true, "ready_count": 0, "managed": 1, "idle": true}
  }
}
```

Overall `status`:
- `"ready"` = every service is serving **and** has at least one warm standby (instant resets).
- `"serving"` = every service is serving, but some standbys are still warming (a reset may briefly return 503).
- `"warming"` = at least one service's active container is not up yet.

Per-service fields: `active_up` (is the active container serving), `ready_count` (warm standbys available now), `managed` (total instances currently kept), `idle` (no reset within the idle timeout — running at min size).

### Other endpoints

```
GET http://localhost:7565/shrink   # force every pool down to its min size now
GET http://localhost:7565/retry    # reconcile now: retry failed, warm/shrink as needed
```

### Response codes

| Code | Meaning |
|------|---------|
| 200 | Reset complete |
| 400 | Unknown service name |
| 503 | No ready standby yet (warming; retry shortly) |

### Tuning

CLI flag (on `server.py` / `07_serve_reset.sh`):

| Flag | Default | Meaning |
|------|---------|---------|
| `--max-concurrent-boots N` | `1` | Max containers to create+start simultaneously across all services. `1` = one podman boot at a time even under many concurrent resets; higher warms faster but loads podman more. (Env fallback: `WEBARENA_MAX_CONCURRENT_BOOTS`.) |

Environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEBARENA_IDLE_TIMEOUT` | `1800` | Seconds without a reset before a service is shrunk to its min size. |
| `WEBARENA_WARMER_INTERVAL` | `15` | How often (seconds) the warmer reconciles pools. |
| `WEBARENA_MAX_CONCURRENT_BOOTS` | `1` | Default for `--max-concurrent-boots` when the flag is omitted. |

## Restarting the server

```bash
# Stop (Ctrl+C or):
sudo kill $(sudo ss -tlnp | grep 7565 | grep -oP 'pid=\K\d+')

# Clean start:
sudo bash 07_serve_reset.sh
```

The `--init` flag recreates all pool containers from the `:ready` images. Without `--init`, the server resumes from `pool_state.json`.

## Updating the baseline

If you want to change the "clean" state that resets restore to:

1. Make changes to the running single-instance containers
2. Re-run `sudo bash 08_checkpoint.sh` to commit new `:ready` images
3. Restart the reset server (it will recreate all pool instances from the new images)

## SSH tunnel for browser access

```bash
ssh -L 8082:localhost:8082 \
    -L 8083:localhost:8083 \
    -L 8080:localhost:8080 \
    -L 8081:localhost:8081 \
    -L 9001:localhost:9001 \
    -L 8443:localhost:443 \
    user@YOUR_SERVER_IP
```

Then open in your browser:
- http://localhost:8082 (shopping)
- http://localhost:8083/admin (shopping admin)
- http://localhost:8080 (forum)
- http://localhost:8081 (wikipedia)
- http://localhost:9001/explore (gitlab)
- http://localhost:8443 (openstreetmap)
