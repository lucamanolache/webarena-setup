> :warning: **This is not an official WebArena repo. For the official instructions refer to [WebArena](https://github.com/web-arena-x/webarena/tree/main/environment_docker)**

# webarena-setup

Setup scripts and hot-swap reset server for WebArena.

## Prerequisites

- Debian 12 server with podman and nginx installed
- Required archive files (see [Get the files](#get-the-files))

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

## Initial setup (one-time)

```bash
# Load images into podman
sudo bash 01_docker_load_images.sh

# Create and start containers
sudo bash 02_docker_remove_containers.sh
sudo bash 03_docker_create_containers.sh
sudo bash 04_docker_start_containers.sh

# Patch containers (configure URLs, etc.)
sudo bash 05_docker_patch_containers.sh

# Commit patched containers as :ready images (used by the pool)
sudo bash 08_checkpoint.sh

# Start homepage server (port 80)
sudo bash 06_serve_homepage.sh
```

## Install nginx

The reset server uses nginx as a reverse proxy to route public ports to pool containers.

```bash
sudo apt install nginx
sudo rm /etc/nginx/sites-enabled/default   # free port 80 for homepage
sudo systemctl enable nginx
sudo systemctl start nginx
```

## Start the reset server

```bash
sudo bash 07_serve_reset.sh
```

This runs `server.py --port 7565 --init` which:
1. Starts static services (OpenStreetMap)
2. Creates a pool of container instances per service
3. Health-checks all instances in parallel
4. Writes nginx config to route public ports to active instances
5. Starts the HTTP API on port 7565

On Ctrl+C or SIGTERM, the server tears down all containers and cleans up nginx.

## Architecture

### Container pool

Each service maintains multiple container instances. One is **active** (serving traffic), the rest are **ready** (standby) or **rebuilding**.

| Service | Public port | Pool size | Boot time |
|---------|------------|-----------|-----------|
| shopping | 8082 | 2 | ~2 min |
| shopping_admin | 8083 | 2 | ~2 min |
| forum | 8080 | 2 | ~1 min |
| gitlab | 9001 | 5 | ~4 min |
| wikipedia | 8081 | 2 | ~10 sec |
| openstreetmap | 443 | 1 (static) | ~30 sec |

Each instance gets a unique host port: `public_port + (index + 1) * 10000`. For example, `shopping_0` listens on 18082, `shopping_1` on 28082.

### Reset flow (~0.05s)

1. Find next ready standby (round-robin)
2. Update nginx config to point public port at new instance
3. `nginx -s reload`
4. Mark old instance as rebuilding, start background rebuild
5. If no standbys remain, auto-spawn an extra instance

### Static services

OpenStreetMap (db + web) is started once and never reset. It is managed by the server (started on init, stopped on teardown) but excluded from the pool/reset cycle.

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
  "status": "ready",
  "services": {
    "shopping": {"active": 0, "ready_count": 1, "total": 2},
    "gitlab": {"active": 2, "ready_count": 3, "total": 5}
  }
}
```

- `"status": "ready"` = every service has at least 1 ready standby
- `"status": "warming"` = some services have 0 ready standbys (reset may fail)

### Response codes

| Code | Meaning |
|------|---------|
| 200 | Reset complete |
| 400 | Unknown service name |
| 503 | No ready standby (extra instance spawned for next time) |

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
    luca@34.72.36.164
```

Then open in your browser:
- http://localhost:8082 (shopping)
- http://localhost:8083/admin (shopping admin)
- http://localhost:8080 (forum)
- http://localhost:8081 (wikipedia)
- http://localhost:9001/explore (gitlab)
- http://localhost:8443 (openstreetmap)
