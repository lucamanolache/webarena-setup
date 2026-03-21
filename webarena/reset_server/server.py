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
import http.server
import json
import logging
import os
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

WORKING_DIR = os.environ.get("WEBARENA_WORKING_DIR", os.path.expanduser("~/webarena-setup/webarena"))

SERVICES = {
    "shopping": {
        "image": "shopping:ready",
        "container_port": 80,
        "public_port": 8082,
        "pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 60},
    },
    "shopping_admin": {
        "image": "shopping_admin:ready",
        "container_port": 80,
        "public_port": 8083,
        "pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 60},
    },
    "forum": {
        "image": "forum:ready",
        "container_port": 80,
        "public_port": 8080,
        "pool_size": 2,
        "create_args": [],
        "health_check": {"type": "exec", "cmd": "curl -sf http://localhost", "timeout": 60},
    },
    "gitlab": {
        "image": "gitlab:ready",
        "container_port": 9001,
        "public_port": 9001,
        "pool_size": 5,
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
        "create_args": [],
        "create_cmd": ["wikipedia_en_all_maxi_2022-05.zim"],
        "create_volumes": {f"{WORKING_DIR}/wiki/": "/data"},
        "health_check": {"type": "http", "url_template": "http://localhost:{host_port}", "timeout": 60},
    },
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
# iptables management
# ---------------------------------------------------------------------------

def _iptables_delete_rules(public_port: int):
    """Remove all existing REDIRECT rules for `public_port` in nat table."""
    for chain in ("PREROUTING", "OUTPUT"):
        while True:
            # List rules with line numbers
            r = subprocess.run(
                ["iptables", "-t", "nat", "-L", chain, "--line-numbers", "-n"],
                capture_output=True, text=True, check=False,
            )
            # Find lines matching our dport
            found = False
            for line in reversed(r.stdout.splitlines()):
                # Lines look like:  "1    REDIRECT  tcp  --  0.0.0.0/0  0.0.0.0/0  tcp dpt:8082 redir ports 18082"
                if f"dpt:{public_port}" in line and "REDIRECT" in line:
                    line_num = line.split()[0]
                    subprocess.run(
                        ["iptables", "-t", "nat", "-D", chain, line_num],
                        capture_output=True, text=True, check=False,
                    )
                    found = True
                    break  # restart scan since line numbers shifted
            if not found:
                break


def set_redirect(public_port: int, target_port: int):
    """Set iptables REDIRECT so traffic to `public_port` goes to `target_port`."""
    _iptables_delete_rules(public_port)

    # PREROUTING: external traffic
    subprocess.run(
        ["iptables", "-t", "nat", "-A", "PREROUTING",
         "-p", "tcp", "--dport", str(public_port),
         "-j", "REDIRECT", "--to-port", str(target_port)],
        check=True,
    )
    # OUTPUT: localhost traffic (for health checks etc.)
    subprocess.run(
        ["iptables", "-t", "nat", "-A", "OUTPUT",
         "-p", "tcp", "-o", "lo", "--dport", str(public_port),
         "-j", "REDIRECT", "--to-port", str(target_port)],
        check=True,
    )
    logger.info("iptables: %d → %d", public_port, target_port)

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
            r = subprocess.run(
                ["podman", "exec", name, "sh", "-c", cmd],
                capture_output=True, text=True, check=False, timeout=15,
            )
            if r.returncode == 0:
                return True
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
        self.public_port = config["public_port"]
        self.lock = threading.Lock()

        if state:
            self.active = state["active"]
            self.instances = {int(k): v for k, v in state["instances"].items()}
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
            self.instances[index] = "failed"
            logger.error("[%s] %s failed health check", self.name, name)

    def get_next_ready(self) -> int | None:
        """Find the next ready instance (round-robin from current active)."""
        for offset in range(1, self.pool_size):
            idx = (self.active + offset) % self.pool_size
            if self.instances.get(idx) == "ready":
                return idx
        return None

    def swap(self) -> tuple[bool, str]:
        """Swap to next ready instance. Returns (success, message)."""
        with self.lock:
            next_idx = self.get_next_ready()
            if next_idx is None:
                return False, f"No ready standby for: {self.name}"

            old_idx = self.active

            # Swap iptables
            set_redirect(self.public_port, self._host_port(next_idx))

            # Update state
            self.instances[next_idx] = "active"
            self.instances[old_idx] = "rebuilding"
            self.active = next_idx

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
            self.instances[index] = "failed"
            logger.error("[%s] %s failed health check after rebuild", self.name, name)

    def ready_count(self) -> int:
        return sum(1 for s in self.instances.values() if s == "ready")

    def status_dict(self) -> dict:
        return {
            "active": self.active,
            "ready_count": self.ready_count(),
            "total": self.pool_size,
            "instances": dict(self.instances),
        }


# ---------------------------------------------------------------------------
# HotSwapServer — HTTP server
# ---------------------------------------------------------------------------

class HotSwapServer:
    def __init__(self, services_config: dict, state_file: str):
        self.services_config = services_config
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

    def init(self):
        """First-time setup: create all pool instances and configure iptables."""
        logger.info("=== Initializing all service pools ===")
        for name, config in self.services_config.items():
            pool = ServicePool(name, config)
            pool.init_all()
            self.pools[name] = pool
        self._save_state()
        logger.info("=== Initialization complete ===")

    def resume(self):
        """Resume from persisted state. Re-establish iptables rules."""
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
        """Swap specified services (or all). Returns (http_status, message)."""
        targets = services if services else list(self.pools.keys())

        # Validate service names
        invalid = [s for s in targets if s not in self.pools]
        if invalid:
            return 400, f"Unknown services: {', '.join(invalid)}"

        errors = []
        for name in targets:
            ok, msg = self.pools[name].swap()
            if not ok:
                errors.append(msg)

        self._save_state()

        if errors:
            return 503, "; ".join(errors)
        return 200, "Reset complete"

    def status(self) -> dict:
        svc_status = {name: pool.status_dict() for name, pool in self.pools.items()}
        all_have_standbys = all(pool.ready_count() > 0 for pool in self.pools.values())
        return {
            "status": "ready" if all_have_standbys else "warming",
            "services": svc_status,
        }


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

        else:
            self._respond(404, {"message": "Not found. Use /reset or /status"})

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

    server_instance = HotSwapServer(SERVICES, args.state_file)

    if args.init:
        server_instance.init()
    else:
        server_instance.resume()

    httpd = http.server.ThreadingHTTPServer(("", args.port), RequestHandler)
    logger.info("Serving on port %d...", args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        httpd.server_close()


if __name__ == "__main__":
    main()
