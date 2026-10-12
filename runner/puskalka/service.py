"""Execute one complete lab and expose its lifecycle to a trusted parent."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .bootstrap import FAT_TEMPLATES, ISO_CONFIG_FILES, prepare_bootstrap
from .model import Lab, SpecificationError
from .network import LinuxNetwork
from .processes import ProcessSupervisor
from .qemu import command, consoles
from .storage import AttemptStorage


LOG = logging.getLogger("puskalka")


class LabExecutor:
    def __init__(self, lab: Lab, runtime: Path = Path("/run/puskalka")):
        self.lab = lab
        self.runtime = runtime
        self.storage = AttemptStorage(lab)
        self.network = LinuxNetwork(lab.networks, lab.ports(), external_networks=lab.ticket.external_networks)
        self.processes = ProcessSupervisor()
        self.lock = threading.RLock()
        self.shutdown = threading.Event()
        self.commands: dict[int, list[str]] = {}
        self.desired: dict[int, bool] = {n.id: True for n in lab.nodes}
        self.errors: dict[int, str] = {}
        self.ready = False

    def prepare(self) -> None:
        for device in ("/dev/kvm", "/dev/net/tun"):
            if not os.access(device, os.R_OK | os.W_OK):
                raise RuntimeError(f"{device} is unavailable; parent must provide this device to the container")
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Reject unsupported config injection before creating any topology.
        for node in self.lab.nodes:
            if node.config and node.template not in ISO_CONFIG_FILES and node.template not in FAT_TEMPLATES:
                raise SpecificationError(f"node {node.id}: automatic startup config unsupported for {node.template!r}")
            command(self.lab, node, self.storage.node_dir(node.id), self.runtime)
        self.storage.acquire()
        self.network.prepare()
        for node in self.lab.nodes:
            self.storage.prepare(node)
            extra = prepare_bootstrap(node.template, self.storage.node_dir(node.id), node.config)
            self.commands[node.id] = command(self.lab, node, self.storage.node_dir(node.id), self.runtime,
                                             bootstrap_args=extra)

    def start(self, node_id: int) -> None:
        with self.lock:
            if node_id not in self.commands:
                raise KeyError(f"unknown node {node_id}")
            if self.shutdown.is_set():
                raise RuntimeError("lab is shutting down")
            self.desired[node_id] = True
            if self.processes.status().get(str(node_id), {}).get("state") == "running":
                self.errors.pop(node_id, None)
                return
            try:
                self.processes.start(node_id, self.commands[node_id], self.storage.node_dir(node_id),
                                     self.runtime / f"qmp-{node_id}.sock",
                                     self.runtime / f"qemu-{node_id}.log")
                self.errors.pop(node_id, None)
            except Exception as exc:
                self.errors[node_id] = str(exc)
                raise

    def start_all(self) -> None:
        for node in self.lab.nodes:
            if self.shutdown.is_set():
                break
            if node.delay and self.shutdown.wait(node.delay):
                break
            try:
                with self.lock:
                    if not self.desired[node.id]:
                        continue
                    self.start(node.id)
                LOG.info(json.dumps({"event": "node-started", "node_id": node.id}))
            except Exception as exc:
                # Keep the other lab nodes available if one cannot boot.
                LOG.error(json.dumps({"event": "node-start-failed", "node_id": node.id, "error": str(exc)}))
        self.ready = True

    def stop(self, node_id: int) -> None:
        with self.lock:
            if node_id not in self.commands:
                raise KeyError(f"unknown node {node_id}")
            self.desired[node_id] = False
            self.processes.stop(node_id)
            self.errors.pop(node_id, None)

    def status(self) -> dict:
        with self.lock:
            processes = self.processes.status()
            entries = {}
            degraded = False
            for node in self.lab.nodes:
                item = dict(processes.get(str(node.id), {"pid": None, "state": "stopped", "exit_code": None}))
                item["desired_running"] = self.desired.get(node.id, False)
                if node.id in self.errors:
                    item["error"] = self.errors[node.id]
                if item["desired_running"] and item["state"] != "running":
                    degraded = True
                entries[str(node.id)] = item
            return {"ticket_id": self.lab.ticket.ticket_id, "attempt_id": self.lab.ticket.attempt_id,
                    "state": "stopping" if self.shutdown.is_set() else
                             "preparing" if not self.ready else "degraded" if degraded else "ready",
                    "nodes": entries}

    def close(self) -> None:
        with self.lock:
            try:
                self.processes.stop_all()
            finally:
                try:
                    self.network.cleanup()
                finally:
                    self.storage.release()
                    self.ready = False


def make_server(executor: LabExecutor, listen: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code: int, payload: dict | list) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/health":
                self.send_json(200, {"ok": True, "state": executor.status()["state"]})
            elif route == "/status":
                self.send_json(200, executor.status())
            elif route == "/consoles":
                self.send_json(200, consoles(executor.lab))
            else:
                self.send_json(404, {"error": "unknown route"})

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            parts = route.strip("/").split("/")
            if route == "/stop":
                executor.shutdown.set()
                self.send_json(202, {"state": "stopping"})
                return
            if len(parts) != 3 or parts[0] != "nodes" or parts[2] not in {"start", "stop"} or not parts[1].isdigit():
                self.send_json(404, {"error": "unknown route"})
                return
            try:
                node_id = int(parts[1])
                getattr(executor, parts[2])(node_id)
                self.send_json(200, executor.status())
            except KeyError as exc:
                self.send_json(404, {"error": str(exc)})
            except Exception as exc:
                LOG.error("node action failed: %s", exc)
                self.send_json(409, {"error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            LOG.info("manager HTTP %s", format % args)

    server = ThreadingHTTPServer((listen, port), Handler)
    server.daemon_threads = True
    return server
