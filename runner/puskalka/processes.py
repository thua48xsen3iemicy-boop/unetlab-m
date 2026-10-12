"""QMP lifecycle control for the QEMU children owned by one lab runner.

Guest agents are not involved: QMP talks to the host-side QEMU process.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class QMPClient:
    """Small synchronous QMP client; unsolicited events are ignored."""

    def __init__(self, path: Path, timeout: float = 3):
        self.path = Path(path)
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.buffer = b""
        self.sequence = 0

    def connect(self) -> "QMPClient":
        if self.socket is not None:
            return self
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.path))
            self.socket = connection
            deadline = time.monotonic() + self.timeout
            while "QMP" not in self._receive(deadline):
                pass
            self._execute("qmp_capabilities", None, deadline)
            return self
        except Exception:
            connection.close()
            self.socket = None
            raise

    def _receive(self, deadline: float) -> dict:
        if self.socket is None:
            raise RuntimeError("QMP is not connected")
        if time.monotonic() >= deadline:
            raise TimeoutError("QMP response timed out")
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QMP response timed out")
            self.socket.settimeout(remaining)
            data = self.socket.recv(65536)
            if not data:
                raise ConnectionError("QMP connection closed")
            self.buffer += data
            if len(self.buffer) > 1024 * 1024:
                raise RuntimeError("QMP response exceeds size limit")
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("QMP response is not an object")
        return message

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        if self.socket is None:
            self.connect()
        return self._execute(command, arguments, time.monotonic() + self.timeout)

    def _execute(self, command: str, arguments: dict | None, deadline: float) -> dict:
        self.sequence += 1
        message = {"execute": command, "id": self.sequence}
        if arguments is not None:
            message["arguments"] = arguments
        assert self.socket is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("QMP response timed out")
        self.socket.settimeout(remaining)
        self.socket.sendall(json.dumps(message).encode() + b"\r\n")
        while True:
            response = self._receive(deadline)
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                error = response["error"]
                raise RuntimeError(f"QMP {command}: {error.get('desc', error)}")
            return response.get("return", {})

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.buffer = b""

    def __enter__(self) -> "QMPClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class _Child:
    process: subprocess.Popen
    qmp_path: Path
    log: BinaryIO
    stopped: bool = False


class ProcessSupervisor:
    """Own and reap only the QEMU subprocesses launched by this instance."""

    def __init__(self):
        self.children: dict[int, _Child] = {}
        self.lock = threading.RLock()

    def start(self, node_id: int, args: list[str], cwd: Path, qmp_path: Path,
              log_path: Path, startup_timeout: float = 15) -> None:
        with self.lock:
            previous = self.children.get(node_id)
            if previous and previous.process.poll() is None:
                raise RuntimeError(f"node {node_id} is already running")
            if previous:
                previous.log.close()
            qmp_path = Path(qmp_path)
            # The runtime directory is owned by this runner, not shared progress.
            qmp_path.unlink(missing_ok=True)
            log = Path(log_path).open("ab", buffering=0)
            try:
                process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT)
            except Exception:
                log.close()
                raise
            child = _Child(process, qmp_path, log)
            self.children[node_id] = child
            deadline = time.monotonic() + max(0, startup_timeout)
            last_error: Exception | None = None
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with QMPClient(qmp_path, timeout=min(1, deadline - time.monotonic())):
                        pass
                    if process.poll() is None:
                        return
                except (OSError, RuntimeError, ValueError) as exc:
                    last_error = exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            self._stop_children([child], 0)
            child.stopped = False
            detail = f"exit code {process.returncode}" if process.returncode is not None else "QMP unavailable"
            raise RuntimeError(f"node {node_id} failed to become ready ({detail}); see {log_path}") from last_error

    def status(self) -> dict[str, dict]:
        with self.lock:
            result = {}
            for node_id, child in self.children.items():
                code = child.process.poll()
                if code is not None:
                    child.log.close()
                result[str(node_id)] = {"pid": child.process.pid,
                    "state": "running" if code is None else "stopped" if child.stopped else "failed",
                    "exit_code": code}
            return result

    @staticmethod
    def _command(child: _Child, command: str, timeout: float = 0.5) -> None:
        if child.process.poll() is not None:
            return
        try:
            # Connection negotiation and the command each receive half the budget.
            with QMPClient(child.qmp_path, timeout=timeout / 2) as client:
                client.execute(command)
        except (OSError, RuntimeError, ValueError):
            # Loss of QMP must not prevent terminating the tracked child.
            pass

    @staticmethod
    def _wait(children: list[_Child], deadline: float) -> list[_Child]:
        pending = [child for child in children if child.process.poll() is None]
        while pending and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            pending = [child for child in pending if child.process.poll() is None]
        return pending

    def _stop_children(self, children: list[_Child], grace_seconds: float) -> None:
        deadline = time.monotonic() + max(0, grace_seconds)
        for child in children:
            child.stopped = True
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._command(child, "system_powerdown", min(0.5, remaining))
        pending = self._wait(children, deadline)
        # All nodes get a shutdown opportunity before forceful escalation.
        quit_deadline = time.monotonic() + 1
        for child in pending:
            remaining = quit_deadline - time.monotonic()
            if remaining > 0:
                self._command(child, "quit", min(0.5, remaining))
        pending = self._wait(pending, quit_deadline)
        for child in pending:
            try:
                child.process.terminate()
            except ProcessLookupError:
                pass
        pending = self._wait(pending, time.monotonic() + 1)
        for child in pending:
            try:
                child.process.kill()
            except ProcessLookupError:
                pass
        for child in children:
            child.process.wait()
            child.log.close()

    def stop(self, node_id: int, grace_seconds: float = 10) -> None:
        with self.lock:
            child = self.children.get(node_id)
            if child is not None:
                self._stop_children([child], grace_seconds)

    def stop_all(self, grace_seconds: float = 10) -> None:
        with self.lock:
            self._stop_children(list(self.children.values()), grace_seconds)
