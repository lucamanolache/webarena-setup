#!/usr/bin/env python3
"""Hot-swap container reset server for Webarena.

Maintains a pool of container instances per service. Resets are near-instant:
swap iptables rules to point at a ready standby, then rebuild the old one in
the background.

Usage:
    python3 server.py --port 7565 --init   # first-time: create all instances + iptables
    python3 server.py --port 7565          # normal start: resume from persisted state
"""

import argparse
import atexit
import http.server
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("hotswap")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
))
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------
# image: image name used with `podman create`
# container_port: port the service listens on *inside* the container
# public_port: port exposed to clients (iptables redirects here)
# pool_size: how many container instances to keep
# create_args: extra args for `podman create` (volumes, env, cmd…)
# health_check: how to verify the container is ready
#   - type "exec": run a command inside the container
#   - type "http": curl a URL from the host

# Auto-detect: reset_server/ is inside webarena/, so go up one level
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKING_DIR = os.environ.get("WEBARENA_WORKING_DIR", os.path.dirname(_SCRIPT_DIR))

SERVICES = {
    "shopping": {
        "image": "shopping:ready",
        "container_port": 80,
        "public_port": 8082,
        "pool_size": 2,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "shopping_admin": {
        "image": "shopping_admin:ready",
        "container_port": 80,
        "public_port": 8083,
        "pool_size": 2,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "forum": {
        "image": "forum:ready",
        "container_port": 80,
        "public_port": 8080,
        "pool_size": 2,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "gitlab": {
        "image": "gitlab:ready",
        "container_port": 9001,
        "public_port": 9001,
        "pool_size": 5,
        "max_pool_size": 6,
        "create_args": [],
        "create_cmd": ["/opt/gitlab/embedded/bin/runsvdir-start"],
        "create_env": {"GITLAB_PORT": "9001"},
        "health_check": {
            "type": "exec",
            "cmd": "curl -so /dev/null -w '%{http_code}' http://localhost:9001 | grep -q '^[23]'",
            "timeout": 360,
        },
    },
    "wikipedia": {
        "image": "wikipedia:ready",
        "container_port": 80,
        "public_port": 8081,
        "pool_size": 2,
        "max_pool_size": 2,
        "create_args": [],
        "create_cmd": ["wikipedia_en_all_maxi_2022-05.zim"],
        "create_volumes": {f"{WORKING_DIR}/wiki/": "/data"},
        "health_check": {"type": "http", "url_template": "http://localhost:{host_port}", "timeout": 60},
    },
}

# Static services: started once, never reset or pooled.
# Each entry is a list of containers that are started together.
STATIC_SERVICES = {
    "openstreetmap": [
        {
            "name": "openstreetmap-website-db-1",
            "image": "openstreetmap-website-db",
            "port_mapping": "54321:5432",
            "extra_args": ["--network", "osm-net", "--network-alias", "db"],
            "env": {"POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_DB": "openstreetmap"},
            "volumes": {"osm-db-data": "/var/lib/postgresql/data"},
            "health_check": None,
        },
        {
            "name": "openstreetmap-website-web-1",
            "image": "openstreetmap-website-web",
            "port_mapping": "443:3000",
            "extra_args": [
                "--network", "osm-net", "--network-alias", "web",
                "-e", "PIDFILE=/tmp/pids/server.pid",
                "--tmpfs", "/tmp/pids/",
            ],
            "volumes": {
                f"{WORKING_DIR}/openstreetmap-website": "/app",
                "osm-web-node-modules": "/app/node_modules",
                "osm-web-tmp": "/app/tmp",
                "osm-web-storage": "/app/storage",
            },
            "cmd": ["bundle", "exec", "rails", "s", "-p", "3000", "-b", "0.0.0.0"],
            "health_check": {"type": "exec", "cmd": "curl -sf http://localhost:3000", "timeout": 120},
        },
    ],
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "pool_state.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def host_port(public_port: int, index: int) -> int:
    """Compute the unique host port for a service instance."""
    return public_port + (index + 1) * 10000


def container_name(service: str, index: int) -> str:
    return f"{service}_{index}"


def run(cmd: list[str], check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command, log it, return result."""
    logger.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)

# ---------------------------------------------------------------------------
# nginx reverse-proxy management
# ---------------------------------------------------------------------------

NGINX_CONF_DIR = "/etc/nginx/conf.d"
NGINX_CONF_FILE = os.path.join(NGINX_CONF_DIR, "webarena-hotswap.conf")

# Track current port mappings so we can write a single config file
_port_mappings: dict[int, int] = {}  # public_port → target_port


def _write_nginx_conf():
    """Write nginx config and reload."""
    blocks = []
    for public_port, target_port in sorted(_port_mappings.items()):
        blocks.append(f"""server {{
    listen {public_port};
    location / {{
        proxy_pass http://127.0.0.1:{target_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffer_size 16k;
        proxy_busy_buffers_size 24k;
        proxy_buffers 8 16k;
    }}
}}""")
    conf = "\n\n".join(blocks) + "\n"
    with open(NGINX_CONF_FILE, "w") as f:
        f.write(conf)
    subprocess.run(["nginx", "-s", "reload"], capture_output=True, check=True)


def set_redirect(public_port: int, target_port: int):
    """Update nginx to proxy `public_port` → `target_port` and reload."""
    _port_mappings[public_port] = target_port
    _write_nginx_conf()
    logger.info("nginx: %d → %d", public_port, target_port)


def cleanup_nginx():
    """Remove our nginx config and reload."""
    if os.path.exists(NGINX_CONF_FILE):
        os.remove(NGINX_CONF_FILE)
        subprocess.run(["nginx", "-s", "reload"], capture_output=True, check=False)
    _port_mappings.clear()

# ---------------------------------------------------------------------------
# ContainerManager — thin wrapper around podman
# ---------------------------------------------------------------------------

class ContainerManager:
    """Manages container lifecycle via podman subprocess calls."""

    def exists(self, name: str) -> bool:
        r = subprocess.run(
            ["podman", "container", "exists", name],
            capture_output=True, text=True, check=False,
        )
        return r.returncode == 0

    def create(self, name: str, image: str, port_mapping: str,
               extra_args: list[str] | None = None,
               cmd: list[str] | None = None,
               env: dict[str, str] | None = None,
               volumes: dict[str, str] | None = None) -> bool:
        args = ["podman", "create", "--name", name, "-p", port_mapping]
        if env:
            for k, v in env.items():
                args += ["--env", f"{k}={v}"]
        if volumes:
            for src, dst in volumes.items():
                args += ["-v", f"{src}:{dst}"]
        if extra_args:
            args += extra_args
        args.append(image)
        if cmd:
            args += cmd
        try:
            run(args, timeout=60)
            logger.info("Created container %s", name)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error("Failed to create %s: %s", name, e)
            return False

    def start(self, name: str) -> bool:
        try:
            run(["podman", "start", name], timeout=60)
            logger.info("Started container %s", name)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error("Failed to start %s: %s", name, e)
            return False

    def stop(self, name: str, timeout: int = 10) -> bool:
        try:
            run(["podman", "stop", "-t", str(timeout), name], check=False, timeout=timeout + 30)
            return True
        except subprocess.TimeoutExpired:
            run(["podman", "kill", name], check=False, timeout=15)
            return True

    def rm(self, name: str) -> bool:
        try:
            run(["podman", "rm", "-f", name], check=False, timeout=30)
            return True
        except subprocess.TimeoutExpired:
            return False

    def health_check_exec(self, name: str, cmd: str, timeout: int = 60) -> bool:
        """Poll `podman exec <name> sh -c <cmd>` until success or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["podman", "exec", name, "sh", "-c", cmd],
                    capture_output=True, text=True, check=False, timeout=15,
                )
                if r.returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(2)
        return False

    def health_check_http(self, url: str, timeout: int = 60) -> bool:
        """Poll a URL from the host until it responds 2xx/3xx."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["curl", "-sf", url],
                    capture_output=True, text=True, check=False, timeout=10,
                )
                if r.returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(2)
        return False


cm = ContainerManager()

# ---------------------------------------------------------------------------
# ServicePool — manages a pool of instances for one service
# ---------------------------------------------------------------------------

class ServicePool:
    """Manages a pool of container instances for one service."""

    def __init__(self, service_name: str, config: dict, state: dict | None = None):
        self.name = service_name
        self.config = config
        self.pool_size = config["pool_size"]
        self.max_pool_size = config.get("max_pool_size", self.pool_size)
        self.public_port = config["public_port"]
        self.lock = threading.Lock()

        if state:
            self.active = state["active"]
            self.instances = {int(k): v for k, v in state["instances"].items()}
            # Restore pool_size from state (may have grown beyond initial)
            if self.instances:
                self.pool_size = max(self.pool_size, max(self.instances.keys()) + 1)
        else:
            self.active = 0
            self.instances = {i: "pending" for i in range(self.pool_size)}

    def state_dict(self) -> dict:
        return {
            "active": self.active,
            "instances": {str(k): v for k, v in self.instances.items()},
        }

    def _host_port(self, index: int) -> int:
        return host_port(self.public_port, index)

    def _container_name(self, index: int) -> str:
        return container_name(self.name, index)

    def _port_mapping(self, index: int) -> str:
        hp = self._host_port(index)
        cp = self.config["container_port"]
        return f"{hp}:{cp}"

    def _create_instance(self, index: int) -> bool:
        name = self._container_name(index)
        return cm.create(
            name=name,
            image=self.config["image"],
            port_mapping=self._port_mapping(index),
            extra_args=self.config.get("create_args"),
            cmd=self.config.get("create_cmd"),
            env=self.config.get("create_env"),
            volumes=self.config.get("create_volumes"),
        )

    def _health_check(self, index: int) -> bool:
        hc = self.config["health_check"]
        name = self._container_name(index)
        timeout = hc.get("timeout", 60)
        if hc["type"] == "exec":
            return cm.health_check_exec(name, hc["cmd"], timeout)
        elif hc["type"] == "http":
            url = hc["url_template"].format(host_port=self._host_port(index))
            return cm.health_check_http(url, timeout)
        return False

    def init_all(self):
        """Create, start, and health-check all instances. Set up iptables for active."""
        logger.info("[%s] Initializing %d instances...", self.name, self.pool_size)
        for i in range(self.pool_size):
            name = self._container_name(i)
            # Clean up any existing container
            cm.stop(name)
            cm.rm(name)
            # Create and start
            if not self._create_instance(i):
                self.instances[i] = "failed"
                continue
            if not cm.start(name):
                self.instances[i] = "failed"
                continue
            self.instances[i] = "starting"

        # Health-check all in parallel
        threads = []
        for i in range(self.pool_size):
            if self.instances[i] == "failed":
                continue
            t = threading.Thread(target=self._init_health_check, args=(i,), name=f"hc-{self.name}-{i}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # Set up iptables for active instance
        if self.instances.get(self.active) == "ready":
            set_redirect(self.public_port, self._host_port(self.active))
            self.instances[self.active] = "active"
        else:
            # Find any ready instance to be active
            for i in range(self.pool_size):
                if self.instances[i] == "ready":
                    self.active = i
                    set_redirect(self.public_port, self._host_port(i))
                    self.instances[i] = "active"
                    break
            else:
                logger.error("[%s] No instances became ready!", self.name)

        logger.info("[%s] Init complete. Active=%d, states=%s", self.name, self.active, self.instances)

    def _init_health_check(self, index: int):
        name = self._container_name(index)
        logger.info("[%s] Health-checking %s...", self.name, name)
        if self._health_check(index):
            self.instances[index] = "ready"
            logger.info("[%s] %s is ready", self.name, name)
        else:
            # Retry once: restart the container and health-check again
            logger.warning("[%s] %s failed health check, restarting and retrying...", self.name, name)
            cm.stop(name)
            cm.start(name)
            if self._health_check(index):
                self.instances[index] = "ready"
                logger.info("[%s] %s is ready after retry", self.name, name)
            else:
                self.instances[index] = "failed"
                logger.error("[%s] %s failed health check after retry", self.name, name)

    def get_next_ready(self) -> int | None:
        """Find the next ready instance (round-robin from current active)."""
        for offset in range(1, self.pool_size):
            idx = (self.active + offset) % self.pool_size
            if self.instances.get(idx) == "ready":
                return idx
        return None

    def _spawn_extra(self):
        """Add a new instance to the pool in the background, respecting max_pool_size."""
        if self.pool_size >= self.max_pool_size:
            logger.warning("[%s] Pool at max size (%d), not spawning extra",
                           self.name, self.max_pool_size)
            # Try to retry a failed instance instead
            self._retry_failed()
            return
        new_idx = self.pool_size
        self.pool_size += 1
        self.instances[new_idx] = "rebuilding"
        logger.info("[%s] Spawning extra instance %d (pool now %d, max %d)",
                    self.name, new_idx, self.pool_size, self.max_pool_size)
        t = threading.Thread(
            target=self._rebuild, args=(new_idx,),
            name=f"spawn-{self.name}-{new_idx}", daemon=True,
        )
        t.start()

    def _retry_failed(self):
        """Retry the first failed instance: health-check first, rebuild only if needed."""
        for idx, state in self.instances.items():
            if state == "failed":
                self.instances[idx] = "rebuilding"
                logger.info("[%s] Retrying failed instance %d", self.name, idx)
                t = threading.Thread(
                    target=self._retry_or_rebuild, args=(idx,),
                    name=f"retry-{self.name}-{idx}", daemon=True,
                )
                t.start()
                return True
        return False

    def _retry_or_rebuild(self, index: int):
        """Check if a failed instance is actually healthy; rebuild only if not."""
        name = self._container_name(index)
        # Try health-checking the existing container first
        if cm.exists(name) and self._health_check(index):
            self.instances[index] = "ready"
            logger.info("[%s] %s is already healthy, marked ready", self.name, name)
            return
        # Not healthy — full rebuild
        self._rebuild(index)

    def swap(self) -> tuple[bool, str]:
        """Swap to next ready instance. Returns (success, message)."""
        with self.lock:
            next_idx = self.get_next_ready()
            if next_idx is None:
                # No standby ready — try retrying a failed instance first,
                # then spawn extra only if under max_pool_size
                if not self._retry_failed():
                    self._spawn_extra()
                return False, f"No ready standby for: {self.name}"

            old_idx = self.active

            # Swap nginx
            set_redirect(self.public_port, self._host_port(next_idx))

            # Update state
            self.instances[next_idx] = "active"
            self.instances[old_idx] = "rebuilding"
            self.active = next_idx

            # If no more standbys after this swap, grow the pool
            if self.ready_count() == 0:
                if not self._retry_failed():
                    self._spawn_extra()

            logger.info("[%s] Swapped %d → %d", self.name, old_idx, next_idx)

        # Rebuild old instance in background
        t = threading.Thread(
            target=self._rebuild, args=(old_idx,),
            name=f"rebuild-{self.name}-{old_idx}", daemon=True,
        )
        t.start()

        return True, "ok"

    def _rebuild(self, index: int):
        """Destroy old container, recreate, start, health-check."""
        name = self._container_name(index)
        logger.info("[%s] Rebuilding %s...", self.name, name)

        cm.stop(name)
        cm.rm(name)

        if not self._create_instance(index):
            self.instances[index] = "failed"
            logger.error("[%s] Failed to create %s", self.name, name)
            return

        if not cm.start(name):
            self.instances[index] = "failed"
            logger.error("[%s] Failed to start %s", self.name, name)
            return

        if self._health_check(index):
            self.instances[index] = "ready"
            logger.info("[%s] %s rebuilt and ready", self.name, name)
        else:
            # Retry once: restart and health-check again
            logger.warning("[%s] %s failed health check after rebuild, restarting...", self.name, name)
            cm.stop(name)
            cm.start(name)
            if self._health_check(index):
                self.instances[index] = "ready"
                logger.info("[%s] %s ready after retry", self.name, name)
            else:
                self.instances[index] = "failed"
                logger.error("[%s] %s failed health check after retry", self.name, name)

    def ready_count(self) -> int:
        return sum(1 for s in self.instances.values() if s == "ready")

    def shrink_to_max(self):
        """Remove failed instances beyond max_pool_size."""
        removed = []
        with self.lock:
            # Collect indices to remove: failed instances with index >= max_pool_size
            to_remove = sorted(
                idx for idx, state in self.instances.items()
                if state == "failed" and idx >= self.config["pool_size"]
            )
            while self.pool_size > self.max_pool_size and to_remove:
                idx = to_remove.pop()
                name = self._container_name(idx)
                cm.stop(name)
                cm.rm(name)
                del self.instances[idx]
                removed.append(idx)
                self.pool_size -= 1
        if removed:
            logger.info("[%s] Shrunk pool: removed instances %s (pool now %d)",
                        self.name, removed, self.pool_size)
        return removed

    def status_dict(self) -> dict:
        return {
            "active": self.active,
            "ready_count": self.ready_count(),
            "total": self.pool_size,
            "max_pool_size": self.max_pool_size,
            "instances": dict(self.instances),
        }


# ---------------------------------------------------------------------------
# HotSwapServer — HTTP server
# ---------------------------------------------------------------------------

class HotSwapServer:
    def __init__(self, services_config: dict, static_services: dict, state_file: str):
        self.services_config = services_config
        self.static_services = static_services
        self.state_file = state_file
        self.pools: dict[str, ServicePool] = {}
        self._save_lock = threading.Lock()

    def _load_state(self) -> dict | None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load state file: %s", e)
        return None

    def _save_state(self):
        with self._save_lock:
            state = {name: pool.state_dict() for name, pool in self.pools.items()}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self.state_file)

    def _ensure_nginx(self):
        """Ensure nginx is running with a clean hotswap config."""
        # Write empty config so nginx doesn't try to bind stale ports
        with open(NGINX_CONF_FILE, "w") as f:
            f.write("# managed by server.py\n")
        # Start nginx if not running, or reload to pick up clean config
        r = subprocess.run(["nginx", "-t"], capture_output=True, check=False)
        if r.returncode != 0:
            logger.error("nginx config test failed: %s", r.stderr)
            return
        subprocess.run(["nginx", "-s", "reload"], capture_output=True, check=False)
        # If reload fails (not running), start it
        subprocess.run(["nginx"], capture_output=True, check=False)
        logger.info("nginx is ready")

    def _init_static_services(self):
        """Start static (non-resettable) services."""
        # Ensure podman network exists for OSM
        subprocess.run(["podman", "network", "create", "osm-net"],
                       capture_output=True, check=False)
        for svc_name, containers in self.static_services.items():
            logger.info("=== Starting static service: %s ===", svc_name)
            for spec in containers:
                name = spec["name"]
                cm.stop(name)
                cm.rm(name)
                ok = cm.create(
                    name=name,
                    image=spec["image"],
                    port_mapping=spec["port_mapping"],
                    extra_args=spec.get("extra_args"),
                    cmd=spec.get("cmd"),
                    env=spec.get("env"),
                    volumes=spec.get("volumes"),
                )
                if not ok:
                    logger.error("Failed to create static container %s", name)
                    continue
                cm.start(name)

            # Health-check static containers
            for spec in containers:
                hc = spec.get("health_check")
                if not hc:
                    continue
                name = spec["name"]
                logger.info("Health-checking static container %s...", name)
                if hc["type"] == "exec":
                    ok = cm.health_check_exec(name, hc["cmd"], hc.get("timeout", 60))
                elif hc["type"] == "http":
                    ok = cm.health_check_http(hc["url"], hc.get("timeout", 60))
                else:
                    ok = False
                if ok:
                    logger.info("  %s ready", name)
                else:
                    logger.error("  %s failed health check", name)

    def _teardown_static_services(self):
        """Stop and remove static service containers."""
        for svc_name, containers in self.static_services.items():
            for spec in containers:
                name = spec["name"]
                logger.info("Stopping static container %s...", name)
                cm.stop(name)
                cm.rm(name)

    def init(self):
        """First-time setup: create all pool instances and configure nginx."""
        self._ensure_nginx()
        self._init_static_services()
        logger.info("=== Initializing all service pools in parallel ===")
        threads = []
        for name, config in self.services_config.items():
            pool = ServicePool(name, config)
            self.pools[name] = pool
            t = threading.Thread(target=pool.init_all, name=f"init-{name}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._save_state()
        logger.info("=== Initialization complete ===")

    def resume(self):
        """Resume from persisted state. Re-establish nginx rules."""
        self._ensure_nginx()
        self._init_static_services()

        saved = self._load_state()
        if not saved:
            logger.error("No state file found. Run with --init first.")
            sys.exit(1)

        for name, config in self.services_config.items():
            state = saved.get(name)
            pool = ServicePool(name, config, state=state)
            # Re-establish iptables for the active instance
            active = pool.active
            if pool.instances.get(active) in ("active", "ready"):
                set_redirect(pool.public_port, pool._host_port(active))
                pool.instances[active] = "active"
            self.pools[name] = pool

        logger.info("Resumed from state file. Services: %s",
                     {n: p.active for n, p in self.pools.items()})

    def reset(self, services: list[str] | None = None) -> tuple[int, str]:
        """Swap specified services (or all). All-or-nothing: only swaps if every
        target service has a ready standby."""
        targets = services if services else list(self.pools.keys())

        # Validate service names
        invalid = [s for s in targets if s not in self.pools]
        if invalid:
            return 400, f"Unknown services: {', '.join(invalid)}"

        # Pre-check: ensure all targets have a ready standby before swapping any
        not_ready = []
        for name in targets:
            pool = self.pools[name]
            if pool.get_next_ready() is None:
                not_ready.append(name)
                # Kick off retry/spawn so they'll be ready next time
                with pool.lock:
                    if not pool._retry_failed():
                        pool._spawn_extra()

        if not_ready:
            self._save_state()
            return 503, f"No ready standby for: {', '.join(not_ready)}"

        # All services have standbys — commit the swap
        for name in targets:
            self.pools[name].swap()

        self._save_state()
        return 200, "Reset complete"

    def status(self) -> dict:
        svc_status = {name: pool.status_dict() for name, pool in self.pools.items()}
        all_have_standbys = all(pool.ready_count() > 0 for pool in self.pools.values())
        return {
            "status": "ready" if all_have_standbys else "warming",
            "services": svc_status,
        }

    def teardown(self):
        """Stop and remove all managed containers."""
        logger.info("=== Tearing down all containers ===")
        for name, pool in self.pools.items():
            for i in range(pool.pool_size):
                cname = pool._container_name(i)
                logger.info("Stopping %s...", cname)
                cm.stop(cname)
                cm.rm(cname)
        cleanup_nginx()
        self._teardown_static_services()
        logger.info("=== Teardown complete ===")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Global reference, set in main
server_instance: HotSwapServer | None = None


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/reset":
            services = None
            if "services" in params:
                services = [s.strip() for s in params["services"][0].split(",") if s.strip()]
            status_code, message = server_instance.reset(services)
            self._respond(status_code, {"message": message})

        elif path == "/status":
            self._respond(200, server_instance.status())

        elif path == "/shrink":
            result = {}
            for name, pool in server_instance.pools.items():
                removed = pool.shrink_to_max()
                if removed:
                    result[name] = f"removed instances {removed}"
            server_instance._save_state()
            self._respond(200, {"message": "Shrink complete", "result": result})

        elif path == "/retry":
            result = {}
            for name, pool in server_instance.pools.items():
                if pool._retry_failed():
                    result[name] = "retrying a failed instance"
            server_instance._save_state()
            self._respond(200, {"message": "Retry triggered", "result": result})

        else:
            self._respond(404, {"message": "Not found. Use /reset, /status, /shrink, or /retry"})

    def _respond(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode())

    def log_message(self, format, *args):
        logger.info("%s %s", self.client_address[0], format % args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global server_instance

    parser = argparse.ArgumentParser(description="Hot-swap container reset server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--init", action="store_true",
                        help="First-time init: create all container instances and set up iptables")
    parser.add_argument("--state-file", default=STATE_FILE, help="Path to state JSON file")
    args = parser.parse_args()

    server_instance = HotSwapServer(SERVICES, STATIC_SERVICES, args.state_file)

    if args.init:
        server_instance.init()
    else:
        server_instance.resume()

    # Ensure containers are cleaned up on exit (Ctrl+C, SIGTERM, etc.)
    _torn_down = False

    def cleanup(*_args):
        nonlocal _torn_down
        if not _torn_down:
            _torn_down = True
            server_instance.teardown()

    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *a: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # ignore SSH disconnect
    atexit.register(cleanup)

    httpd = http.server.ThreadingHTTPServer(("", args.port), RequestHandler)
    httpd.allow_reuse_address = False
    logger.info("Serving on port %d...", args.port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
