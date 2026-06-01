#!/usr/bin/env python3
"""Hot-swap container reset server for Webarena.

Maintains a pool of container instances per service. Resets are near-instant:
swap the nginx upstream to a ready standby, then rebuild the old one in the
background.

Design goals (see README "Architecture"):
  * Robust against flaky podman: every container *boot* (create + start) goes
    through a single global lock, so we never launch more than one container at
    a time even when many resets arrive at once. Transient podman errors are
    retried with backoff.
  * Frugal with resources: a service only keeps warm standbys while it is being
    used. Services left idle past IDLE_TIMEOUT shrink back to just their active
    container. A background warmer thread reconciles each pool toward its
    desired size.
  * Fast to come up: --init boots only the active container per service, so the
    stack starts serving quickly; standbys warm in the background.

Usage:
    python3 server.py --port 7565 --init   # first-time: create actives + nginx
    python3 server.py --port 7565          # normal start: resume from state
"""

import argparse
import atexit
import http.server
import json
import logging
import os
import signal
import socket
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
# public_port: port exposed to clients (nginx proxies here)
# warm_target (pool_size): standbys+active to keep warm while the service is
#   being used. Reset hot-swaps to a standby, so warm_target>=2 gives instant
#   resets.
# min_pool_size: instances to keep when the service has been idle past
#   IDLE_TIMEOUT (1 == just the active container, nothing wasted).
# max_pool_size: hard cap on instances (also bounds the port range needed).
# port_range_size: size of the reserved host-port range for this service.
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
        "port_base": 18280,
        "port_range_size": 16,
        "pool_size": 2,
        "min_pool_size": 1,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "shopping_admin": {
        "image": "shopping_admin:ready",
        "container_port": 80,
        "public_port": 8083,
        "port_base": 18380,
        "port_range_size": 16,
        "pool_size": 2,
        "min_pool_size": 1,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "forum": {
        "image": "forum:ready",
        "container_port": 80,
        "public_port": 8080,
        "port_base": 18080,
        "port_range_size": 16,
        "pool_size": 2,
        "min_pool_size": 1,
        "max_pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 360},
    },
    "gitlab": {
        "image": "gitlab:ready",
        "container_port": 9001,
        "public_port": 9001,
        "port_base": 19001,
        "port_range_size": 16,
        "pool_size": 5,
        "min_pool_size": 1,
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
}

# Static services: started once, never reset or pooled.
# Each entry is a list of containers that are started together.
STATIC_SERVICES = {
    "wikipedia": [
        {
            "name": "wikipedia",
            "image": "ghcr.io/kiwix/kiwix-serve:3.3.0",
            "port_mapping": "8081:80",
            "volumes": {f"{WORKING_DIR}/wiki/": "/data"},
            "cmd": ["wikipedia_en_all_maxi_2022-05.zim"],
            "health_check": {"type": "http", "url": "http://localhost:8081", "timeout": 60},
        },
    ],
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
MAX_TCP_PORT = 65535

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Limit the expensive part of bringing a container up (podman create + start)
# across the whole process. podman is happy serving traffic from many
# containers, but launching several at once is a common source of transient
# failures ("database is locked", OOM during boot, etc). Health checks are NOT
# held under this semaphore, so multiple services still warm concurrently.
#
# The number of simultaneous boots is configurable via --max-concurrent-boots
# (default 1 == only one podman create+start at a time, even under many
# concurrent resets). main() rebuilds this with the configured value at startup.
MAX_CONCURRENT_BOOTS = int(os.environ.get("WEBARENA_MAX_CONCURRENT_BOOTS", "1"))
_BOOT_LOCK = threading.BoundedSemaphore(MAX_CONCURRENT_BOOTS)

# nginx config is a single shared file + reload; serialize writers.
_NGINX_LOCK = threading.Lock()

# A service with no reset for this many seconds is considered idle and is
# shrunk back to min_pool_size (just its active container).
IDLE_TIMEOUT = int(os.environ.get("WEBARENA_IDLE_TIMEOUT", str(30 * 60)))

# How often the background warmer reconciles pools toward their desired size.
WARMER_INTERVAL = int(os.environ.get("WEBARENA_WARMER_INTERVAL", "15"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def preferred_host_port(config: dict, index: int) -> int:
    """Compute the preferred host port for a service instance."""
    return config["port_base"] + index


def port_range(config: dict) -> range:
    return range(config["port_base"], config["port_base"] + config["port_range_size"])


def is_port_free(port: int) -> bool:
    """Best-effort local port availability check."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def is_port_conflict_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    lowered = error_text.lower()
    return (
        "address already in use" in lowered
        or "port is already allocated" in lowered
        or ("bind" in lowered and "use" in lowered)
    )


_TRANSIENT_MARKERS = (
    "database is locked",
    "temporarily unavailable",
    "resource temporarily unavailable",
    "layer not known",
    "error acquiring lock",
    "connection refused",
    "cannot connect",
    "timed out",
    "timeout",
    "device or resource busy",
    "no space left on device",  # may clear after a previous boot finishes/cleans
)


def is_transient_error(error_text: str | None) -> bool:
    """Heuristic: should we retry this podman failure?"""
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def validate_services_config():
    """Fail fast if configured service port ranges are invalid or overlap."""
    reserved_ports = {7565}
    reserved_ports.update(config["public_port"] for config in SERVICES.values())
    for containers in STATIC_SERVICES.values():
        for spec in containers:
            host_port = int(spec["port_mapping"].split(":", 1)[0])
            reserved_ports.add(host_port)
    claimed_ports: dict[int, str] = {}

    for service_name, config in SERVICES.items():
        max_instances = config.get("max_pool_size", config["pool_size"])
        min_instances = config.get("min_pool_size", 1)
        if min_instances < 1:
            raise ValueError(f"{service_name}: min_pool_size must be >= 1")
        if min_instances > max_instances:
            raise ValueError(
                f"{service_name}: min_pool_size={min_instances} exceeds "
                f"max_pool_size={max_instances}"
            )
        range_size = config["port_range_size"]
        if range_size < max_instances:
            raise ValueError(
                f"{service_name}: port_range_size={range_size} is smaller than "
                f"max_pool_size={max_instances}"
            )
        for port in port_range(config):
            if port > MAX_TCP_PORT:
                raise ValueError(f"{service_name}: host port {port} exceeds {MAX_TCP_PORT}")
            if port in reserved_ports:
                raise ValueError(f"{service_name}: host port {port} conflicts with a reserved port")
            owner = claimed_ports.get(port)
            if owner:
                raise ValueError(f"{service_name}: host port {port} overlaps with {owner}")
            claimed_ports[port] = service_name


def container_name(service: str, index: int) -> str:
    return f"{service}_{index}"


def run(cmd: list[str], check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command, log it, return result."""
    logger.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


validate_services_config()

# ---------------------------------------------------------------------------
# nginx reverse-proxy management
# ---------------------------------------------------------------------------

NGINX_CONF_DIR = "/etc/nginx/conf.d"
NGINX_CONF_FILE = os.path.join(NGINX_CONF_DIR, "webarena-hotswap.conf")

# Track current port mappings so we can write a single config file
_port_mappings: dict[int, int] = {}  # public_port → target_port


def _write_nginx_conf():
    """Write nginx config and reload. Caller must hold _NGINX_LOCK."""
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
    with _NGINX_LOCK:
        _port_mappings[public_port] = target_port
        _write_nginx_conf()
    logger.info("nginx: %d → %d", public_port, target_port)


def cleanup_nginx():
    """Remove our nginx config and reload."""
    with _NGINX_LOCK:
        if os.path.exists(NGINX_CONF_FILE):
            os.remove(NGINX_CONF_FILE)
            subprocess.run(["nginx", "-s", "reload"], capture_output=True, check=False)
        _port_mappings.clear()

# ---------------------------------------------------------------------------
# ContainerManager — thin wrapper around podman
# ---------------------------------------------------------------------------

class ContainerManager:
    """Manages container lifecycle via podman subprocess calls.

    create() and start() retry transient podman failures with backoff so a
    single flaky launch doesn't permanently fail an instance.
    """

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
               volumes: dict[str, str] | None = None,
               attempts: int = 3) -> tuple[bool, str | None]:
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

        for attempt in range(1, attempts + 1):
            try:
                run(args, timeout=90)
                logger.info("Created container %s", name)
                return True, None
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                stderr = getattr(e, "stderr", None)
                stdout = getattr(e, "stdout", None)
                details = stderr or stdout or str(e)
                # Port conflicts must bubble up so the caller can pick another port.
                if is_port_conflict_error(details):
                    return False, details
                retryable = isinstance(e, subprocess.TimeoutExpired) or is_transient_error(details)
                if attempt < attempts and retryable:
                    logger.warning("Create %s failed (attempt %d/%d, transient): %s",
                                   name, attempt, attempts, str(details).strip()[:200])
                    # Clear any half-created container before retrying.
                    subprocess.run(["podman", "rm", "-f", name],
                                   capture_output=True, check=False, timeout=30)
                    time.sleep(2 * attempt)
                    continue
                logger.error("Failed to create %s: %s", name, details)
                return False, details
        return False, "create failed"

    def start(self, name: str, attempts: int = 3) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                run(["podman", "start", name], timeout=90)
                logger.info("Started container %s", name)
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                stderr = getattr(e, "stderr", None)
                details = stderr or str(e)
                if attempt < attempts:
                    logger.warning("Start %s failed (attempt %d/%d): %s",
                                   name, attempt, attempts, str(details).strip()[:200])
                    time.sleep(2 * attempt)
                    continue
                logger.error("Failed to start %s: %s", name, details)
                return False
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

    def get_host_port(self, name: str, container_port: int) -> int | None:
        try:
            result = run(["podman", "port", name, str(container_port)], timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        output = result.stdout.strip()
        if not output:
            return None
        last_field = output.split()[-1]
        try:
            return int(last_field.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return None

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
    """Manages a pool of container instances for one service.

    Instance states:
      "active"     — currently serving traffic (exactly one).
      "ready"      — warm standby, can be swapped to instantly.
      "rebuilding" — boot in progress (counts toward the pool, not swappable).
      "failed"     — boot failed; the warmer will retry it.

    Only instances we intend to keep appear in `self.instances`; removing an
    instance deletes its key. Pool sizing is usage-based:
      * `warm_target` warm instances while the service is being used,
      * shrunk to `min_size` once idle past IDLE_TIMEOUT,
      * never above `max_pool_size`.
    The background warmer reconciles toward this target; boots are serialized
    process-wide via _BOOT_LOCK.
    """

    def __init__(self, service_name: str, config: dict, state: dict | None = None):
        self.name = service_name
        self.config = config
        # warm_target: warm instances to keep while in active use.
        self.warm_target = config["pool_size"]
        # min_size: warm instances to keep when idle (1 == just the active).
        self.min_size = config.get("min_pool_size", 1)
        self.max_pool_size = config.get("max_pool_size", self.warm_target)
        self.public_port = config["public_port"]
        self.port_base = config["port_base"]
        self.port_range_size = config["port_range_size"]
        # Reentrant: reconcile()/swap() hold the lock while calling helpers and
        # rebuild callbacks re-take it to publish final state.
        self.lock = threading.RLock()

        if state:
            self.active = state.get("active", 0)
            self.instances = {int(k): v for k, v in state.get("instances", {}).items()}
            self.ports = {int(k): int(v) for k, v in state.get("ports", {}).items()}
            # last_used drives idle-shrink. Default to "long ago" so a resumed
            # but unused service stays minimal until something resets it.
            self.last_used = float(state.get("last_used", 0.0))
        else:
            self.active = 0
            self.instances = {0: "pending"}
            self.ports = {}
            # 0.0 == "never used": a freshly-initialised service stays at
            # min_size (active only) until its first reset.
            self.last_used = 0.0

        self._hydrate_ports_from_runtime()

    # -- persistence --------------------------------------------------------

    def state_dict(self) -> dict:
        return {
            "active": self.active,
            "instances": {str(k): v for k, v in self.instances.items()},
            "ports": {str(k): v for k, v in self.ports.items()},
            "last_used": self.last_used,
        }

    def _hydrate_ports_from_runtime(self):
        """Keep persisted port assignments aligned with running containers."""
        for index in self.instances:
            if index in self.ports:
                continue
            host_port = cm.get_host_port(self._container_name(index), self.config["container_port"])
            if host_port is not None:
                self.ports[index] = host_port

    # -- naming / ports -----------------------------------------------------

    def _preferred_host_port(self, index: int) -> int:
        return preferred_host_port(self.config, index)

    def _host_port(self, index: int) -> int:
        return self.ports.get(index, self._preferred_host_port(index))

    def _container_name(self, index: int) -> str:
        return container_name(self.name, index)

    def _port_mapping(self, host_port: int) -> str:
        cp = self.config["container_port"]
        return f"{host_port}:{cp}"

    def _candidate_ports(self, index: int) -> list[int]:
        preferred = self._host_port(index)
        in_use_by_service = {
            port for idx, port in self.ports.items()
            if idx != index and idx in self.instances
        }
        candidates = []
        if preferred in port_range(self.config) and preferred not in in_use_by_service:
            candidates.append(preferred)
        for port in port_range(self.config):
            if port == preferred or port in in_use_by_service:
                continue
            candidates.append(port)
        return candidates

    def _next_index(self) -> int | None:
        """Smallest unused instance index within the reserved range."""
        for i in range(self.port_range_size):
            if i not in self.instances:
                return i
        return None

    # -- counts -------------------------------------------------------------

    def managed_count(self) -> int:
        return len(self.instances)

    def ready_count(self) -> int:
        return sum(1 for s in self.instances.values() if s == "ready")

    def _desired_warm(self) -> int:
        """How many warm instances we want right now, based on recent usage."""
        idle = (time.time() - self.last_used) > IDLE_TIMEOUT
        desired = self.min_size if idle else self.warm_target
        return max(self.min_size, min(desired, self.max_pool_size))

    def mark_used(self):
        with self.lock:
            self.last_used = time.time()

    # -- container boot (serialized) ---------------------------------------

    def _create_instance(self, index: int) -> bool:
        """Create (not start) the container, picking a free host port. Must be
        called under _BOOT_LOCK so concurrent boots don't race on ports."""
        name = self._container_name(index)
        for host_port in self._candidate_ports(index):
            if not is_port_free(host_port):
                continue
            ok, error_text = cm.create(
                name=name,
                image=self.config["image"],
                port_mapping=self._port_mapping(host_port),
                extra_args=self.config.get("create_args"),
                cmd=self.config.get("create_cmd"),
                env=self.config.get("create_env"),
                volumes=self.config.get("create_volumes"),
            )
            if ok:
                self.ports[index] = host_port
                return True
            if not is_port_conflict_error(error_text):
                return False

        logger.error("[%s] No free host ports available in reserved range %d-%d",
                     self.name, self.port_base, self.port_base + self.port_range_size - 1)
        self.ports.pop(index, None)
        return False

    def _health_check(self, index: int, timeout: int | None = None) -> bool:
        hc = self.config["health_check"]
        name = self._container_name(index)
        t = timeout if timeout is not None else hc.get("timeout", 60)
        if hc["type"] == "exec":
            return cm.health_check_exec(name, hc["cmd"], t)
        elif hc["type"] == "http":
            url = hc["url"].format(host_port=self._host_port(index))
            return cm.health_check_http(url, t)
        return False

    def _boot(self, index: int) -> bool:
        """Bring an instance up: (re)create + start under the global boot lock,
        then health-check outside the lock. Returns True if healthy.

        Only the create+start burst is serialized — health checks (the slow
        part) overlap across services so warming stays fast."""
        name = self._container_name(index)
        with _BOOT_LOCK:
            cm.stop(name)
            cm.rm(name)
            if not self._create_instance(index):
                return False
            if not cm.start(name):
                return False

        if self._health_check(index):
            return True

        # Retry once: restart and health-check again (boot serialized).
        logger.warning("[%s] %s failed health check, restarting and retrying...", self.name, name)
        with _BOOT_LOCK:
            cm.stop(name)
            cm.start(name)
        return self._health_check(index)

    def _rebuild(self, index: int):
        """Boot an instance in the current thread and publish its final state.

        If it is (still) the active instance, restore the nginx redirect."""
        name = self._container_name(index)
        logger.info("[%s] Building %s...", self.name, name)
        ok = self._boot(index)
        with self.lock:
            if ok:
                if index == self.active:
                    self.instances[index] = "active"
                    set_redirect(self.public_port, self._host_port(index))
                    logger.info("[%s] %s rebuilt (active)", self.name, name)
                else:
                    self.instances[index] = "ready"
                    logger.info("[%s] %s ready", self.name, name)
            else:
                self.instances[index] = "failed"
                logger.error("[%s] %s failed to build", self.name, name)

    def _spawn_rebuild(self, index: int):
        t = threading.Thread(
            target=self._rebuild, args=(index,),
            name=f"boot-{self.name}-{index}", daemon=True,
        )
        t.start()

    # -- lifecycle ----------------------------------------------------------

    def init_active(self):
        """Boot only the active instance. Standbys warm later, on demand."""
        self.active = 0
        self.ports.pop(0, None)
        self.instances = {0: "rebuilding"}
        name = self._container_name(0)
        logger.info("[%s] Booting active instance %s...", self.name, name)
        if self._boot(0):
            with self.lock:
                self.instances[0] = "active"
            set_redirect(self.public_port, self._host_port(0))
            logger.info("[%s] active ready on host port %d", self.name, self._host_port(0))
        else:
            with self.lock:
                self.instances[0] = "failed"
            logger.error("[%s] active instance failed to boot!", self.name)

    def resume_active(self):
        """On resume: keep/verify the active container, forget standbys, and let
        the warmer re-create standbys as usage dictates. Leftover standby
        containers from the previous run are removed to free resources."""
        with self.lock:
            active = self.active
            for idx in list(self.instances):
                if idx != active:
                    del self.instances[idx]
                    self.ports.pop(idx, None)

        # Best-effort cleanup of any leftover standby containers.
        for i in range(self.port_range_size):
            if i != self.active:
                cm.rm(self._container_name(i))

        name = self._container_name(self.active)
        if cm.exists(name) and self._health_check(self.active, timeout=10):
            with self.lock:
                self.instances[self.active] = "active"
            set_redirect(self.public_port, self._host_port(self.active))
            logger.info("[%s] resumed active %s", self.name, name)
            return

        logger.warning("[%s] active %s not healthy on resume, rebuilding", self.name, name)
        with self.lock:
            self.instances = {self.active: "rebuilding"}
        self._rebuild(self.active)

    # -- swap (reset) -------------------------------------------------------

    def get_next_ready(self) -> int | None:
        """Find the next ready standby (round-robin from the active instance)."""
        with self.lock:
            keys = sorted(self.instances.keys())
            if not keys:
                return None
            start = keys.index(self.active) if self.active in keys else -1
            n = len(keys)
            for offset in range(1, n + 1):
                idx = keys[(start + offset) % n]
                if idx == self.active:
                    continue
                if self.instances.get(idx) == "ready":
                    return idx
        return None

    def swap(self) -> tuple[bool, str]:
        """Swap to the next ready standby and rebuild the old active.

        Returns (success, message). Marks the service used so the warmer keeps
        it warm; if no standby is ready, returns False (the warmer will warm one
        for next time)."""
        old_idx = None
        with self.lock:
            self.last_used = time.time()
            next_idx = self.get_next_ready()
            if next_idx is None:
                return False, f"No ready standby for: {self.name}"

            old_idx = self.active
            set_redirect(self.public_port, self._host_port(next_idx))
            self.instances[next_idx] = "active"
            self.instances[old_idx] = "rebuilding"
            self.active = next_idx
            logger.info("[%s] Swapped %d → %d", self.name, old_idx, next_idx)

        # Rebuild the old instance in the background (serialized via boot lock).
        self._spawn_rebuild(old_idx)
        return True, "ok"

    # -- reconciliation (the warmer) ---------------------------------------

    def reconcile(self):
        """Move the pool toward its desired warm size.

        Called periodically by the warmer thread:
          1. self-heal a failed active container,
          2. shrink excess standbys when idle (frees resources),
          3. retry remaining failed standbys,
          4. grow warm standbys up to the desired size (only when in use).
        All boots are spawned as background threads; _BOOT_LOCK ensures only one
        container is actually created+started at a time."""
        to_spawn: list[int] = []
        with self.lock:
            desired = self._desired_warm()

            # 1. Critical: a dead active must come back no matter what.
            if self.instances.get(self.active) == "failed":
                self.instances[self.active] = "rebuilding"
                to_spawn.append(self.active)

            # 2. Shrink excess warm/failed standbys (idle → free resources).
            if self.managed_count() > desired:
                self._shrink_to(desired)

            # 3. Retry the failed standbys we still intend to keep.
            for idx, st in sorted(self.instances.items()):
                if st == "failed":
                    self.instances[idx] = "rebuilding"
                    to_spawn.append(idx)

            # 4. Grow toward desired (no-op for idle services).
            while self.managed_count() < desired:
                idx = self._next_index()
                if idx is None:
                    break
                self.instances[idx] = "rebuilding"
                to_spawn.append(idx)
                logger.info("[%s] Warming new instance %d (target %d)",
                            self.name, idx, desired)

        for idx in to_spawn:
            self._spawn_rebuild(idx)

    def _shrink_to(self, target: int) -> list[int]:
        """Remove removable (ready/failed, non-active) standbys, highest index
        first, until managed_count() <= target. Caller holds self.lock."""
        removed = []
        while self.managed_count() > target:
            candidates = sorted(
                (idx for idx, st in self.instances.items()
                 if idx != self.active and st in ("ready", "failed")),
                reverse=True,
            )
            if not candidates:
                break  # only active / in-flight boots remain; can't shrink now
            idx = candidates[0]
            name = self._container_name(idx)
            cm.stop(name)
            cm.rm(name)
            del self.instances[idx]
            self.ports.pop(idx, None)
            removed.append(idx)
        if removed:
            logger.info("[%s] Shrunk pool: removed instances %s (now %d)",
                        self.name, removed, self.managed_count())
        return removed

    def shrink_idle(self) -> list[int]:
        """Force-shrink to min_size (used by the /shrink endpoint)."""
        with self.lock:
            return self._shrink_to(self.min_size)

    # -- introspection ------------------------------------------------------

    def status_dict(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "active_up": self.instances.get(self.active) == "active",
                "ready_count": self.ready_count(),
                "managed": self.managed_count(),
                "desired_warm": self._desired_warm(),
                "warm_target": self.warm_target,
                "min_size": self.min_size,
                "max_pool_size": self.max_pool_size,
                "idle": (time.time() - self.last_used) > IDLE_TIMEOUT,
                "instances": dict(self.instances),
                "ports": dict(self.ports),
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
        self._stop = threading.Event()
        self._warmer: threading.Thread | None = None

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
        r = subprocess.run(["nginx", "-t"], capture_output=True, check=False)
        if r.returncode != 0:
            logger.error("nginx config test failed: %s", r.stderr)
            return
        # Kill all nginx processes to guarantee old port bindings are released
        subprocess.run(["pkill", "-9", "nginx"], capture_output=True, check=False)
        time.sleep(1)
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
                # Static boots also go through the global boot lock.
                with _BOOT_LOCK:
                    ok, _ = cm.create(
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

    def _kill_all_containers(self):
        """Stop and remove all podman containers before starting fresh."""
        logger.info("=== Killing all existing podman containers ===")
        subprocess.run(["podman", "stop", "-a", "-t", "10"],
                       capture_output=True, check=False, timeout=120)
        subprocess.run(["podman", "rm", "-a", "-f"],
                       capture_output=True, check=False, timeout=60)
        logger.info("All containers removed")

    def init(self):
        """First-time setup: boot one active per service, configure nginx.

        Standbys are NOT booted here — they warm on demand (first reset), which
        makes the stack start serving in a fraction of the old time."""
        self._kill_all_containers()
        self._ensure_nginx()
        self._init_static_services()
        logger.info("=== Booting active instances ===")
        for name, config in self.services_config.items():
            self.pools[name] = ServicePool(name, config)
        # Boot actives concurrently; _BOOT_LOCK serializes the podman work while
        # health checks overlap, so this is as fast as podman safely allows.
        threads = []
        for pool in self.pools.values():
            t = threading.Thread(target=pool.init_active,
                                 name=f"init-{pool.name}", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._save_state()
        logger.info("=== Active instances up; standbys will warm on demand ===")

    def resume(self):
        """Resume from persisted state. Verify actives, re-establish nginx, and
        let the warmer re-create standbys as usage dictates."""
        self._ensure_nginx()
        self._init_static_services()

        saved = self._load_state()
        if not saved:
            logger.error("No state file found. Run with --init first.")
            sys.exit(1)

        for name, config in self.services_config.items():
            state = saved.get(name)
            self.pools[name] = ServicePool(name, config, state=state)

        threads = []
        for pool in self.pools.values():
            t = threading.Thread(target=pool.resume_active,
                                 name=f"resume-{pool.name}", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._save_state()
        logger.info("Resumed. Services: %s",
                    {n: p.active for n, p in self.pools.items()})

    def reset(self, services: list[str] | None = None) -> tuple[int, str]:
        """Swap specified services (or all). All-or-nothing: only swaps if every
        target service has a ready standby. Marks targets used either way so the
        warmer keeps/brings them warm."""
        targets = services if services else list(self.pools.keys())

        # Validate service names
        invalid = [s for s in targets if s not in self.pools]
        if invalid:
            return 400, f"Unknown services: {', '.join(invalid)}"

        # Mark all targets used so the warmer keeps them warm (even on 503).
        for name in targets:
            self.pools[name].mark_used()

        # Pre-check: ensure all targets have a ready standby before swapping any.
        not_ready = [name for name in targets if self.pools[name].get_next_ready() is None]
        if not_ready:
            self._save_state()
            return 503, (f"No ready standby for: {', '.join(not_ready)} "
                         f"(warming, retry shortly)")

        # All services have standbys — commit the swap. A concurrent reset
        # could still steal a standby between the pre-check and here, so honor
        # each swap's result.
        failed = [name for name in targets if not self.pools[name].swap()[0]]
        self._save_state()
        if failed:
            return 503, (f"No ready standby for: {', '.join(failed)} "
                         f"(warming, retry shortly)")
        return 200, "Reset complete"

    def status(self) -> dict:
        svc_status = {name: pool.status_dict() for name, pool in self.pools.items()}
        # "ready" = every service is actively serving. Standby availability is
        # reported per-service; idle services intentionally keep no standby.
        all_active_up = all(s["active_up"] for s in svc_status.values())
        all_have_standbys = all(s["ready_count"] > 0 for s in svc_status.values())
        if not all_active_up:
            overall = "warming"
        elif all_have_standbys:
            overall = "ready"
        else:
            overall = "serving"  # actives up, some standbys still warming
        return {"status": overall, "services": svc_status}

    def start_warmer(self):
        """Start the background reconciliation loop."""
        if self._warmer and self._warmer.is_alive():
            return
        self._warmer = threading.Thread(target=self._warmer_loop,
                                        name="warmer", daemon=True)
        self._warmer.start()
        logger.info("Warmer started (interval=%ds, idle_timeout=%ds)",
                    WARMER_INTERVAL, IDLE_TIMEOUT)

    def _warmer_loop(self):
        while not self._stop.is_set():
            try:
                for pool in list(self.pools.values()):
                    pool.reconcile()
                self._save_state()
            except Exception:
                logger.exception("warmer iteration failed")
            self._stop.wait(WARMER_INTERVAL)

    def teardown(self):
        """Stop and remove all managed containers."""
        logger.info("=== Tearing down all containers ===")
        self._stop.set()
        for name, pool in self.pools.items():
            # Remove every index in the reserved range to catch leftovers too.
            for i in range(pool.port_range_size):
                cname = pool._container_name(i)
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
                removed = pool.shrink_idle()
                if removed:
                    result[name] = f"removed instances {removed}"
            server_instance._save_state()
            self._respond(200, {"message": "Shrink complete", "result": result})

        elif path == "/retry":
            # Kick a reconcile so failed instances are retried immediately.
            for pool in server_instance.pools.values():
                pool.reconcile()
            server_instance._save_state()
            self._respond(200, {"message": "Reconcile triggered"})

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
                        help="First-time init: boot active instances and set up nginx")
    parser.add_argument("--state-file", default=STATE_FILE, help="Path to state JSON file")
    parser.add_argument(
        "--max-concurrent-boots", type=int, default=MAX_CONCURRENT_BOOTS,
        metavar="N",
        help="Max containers to create+start simultaneously across all services "
             "(default %(default)s). 1 == one podman boot at a time even under "
             "many concurrent resets; higher warms faster but loads podman more. "
             "Env: WEBARENA_MAX_CONCURRENT_BOOTS.",
    )
    args = parser.parse_args()

    if args.max_concurrent_boots < 1:
        parser.error("--max-concurrent-boots must be >= 1")

    # Size the global boot semaphore before anything boots (init/resume below).
    global _BOOT_LOCK
    _BOOT_LOCK = threading.BoundedSemaphore(args.max_concurrent_boots)
    logger.info("Max concurrent podman boots: %d", args.max_concurrent_boots)

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

    # Kill any old server on this port and wait for it to release
    r = subprocess.run(["ss", "-tlnp", f"sport = :{args.port}"],
                       capture_output=True, text=True, check=False)
    for line in r.stdout.splitlines():
        if f":{args.port}" in line:
            import re
            m = re.search(r"pid=(\d+)", line)
            if m:
                old_pid = int(m.group(1))
                if old_pid != os.getpid():
                    logger.warning("Killing old server on port %d (pid %d)", args.port, old_pid)
                    os.kill(old_pid, signal.SIGTERM)
                    # Wait for port to be released
                    for _ in range(30):
                        time.sleep(1)
                        r2 = subprocess.run(["ss", "-tlnp", f"sport = :{args.port}"],
                                            capture_output=True, text=True, check=False)
                        if f":{args.port}" not in r2.stdout:
                            break
                    else:
                        logger.warning("Force-killing old server (pid %d)", old_pid)
                        os.kill(old_pid, signal.SIGKILL)
                        time.sleep(2)

    # Start the background warmer (lazy standby warming + idle shrink).
    server_instance.start_warmer()

    httpd = http.server.ThreadingHTTPServer(("", args.port), RequestHandler)
    logger.info("Serving on port %d...", args.port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
