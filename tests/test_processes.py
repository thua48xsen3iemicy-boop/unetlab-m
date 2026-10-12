"""Lifecycle checks with actual subprocesses and Unix QMP sockets."""

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from puskalka.processes import ProcessSupervisor, QMPClient


FAKE_QEMU = r'''
import json, signal, socket, sys, time
path, commands, mode, node = sys.argv[1:]
if mode == "fail":
    print("invalid machine", flush=True)
    sys.exit(7)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(4)
while True:
    stream, _ = server.accept()
    with stream:
        stream.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
        reader = stream.makefile("rb")
        for line in reader:
            request = json.loads(line)
            command = request["execute"]
            with open(commands, "a") as log:
                log.write(json.dumps([node, command, time.monotonic()]) + "\n")
            stream.sendall(json.dumps({"event":"TEST"}).encode() + b"\r\n")
            response = {"id":request["id"], "return":{"running":True} if command == "query-status" else {}}
            stream.sendall(json.dumps(response).encode() + b"\r\n")
            if (command == "system_powerdown" and mode == "graceful") or (command == "quit" and mode != "stubborn"):
                sys.exit(0)
'''


class QMPTests(unittest.TestCase):
    def test_events_wrong_ids_error_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qmp.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            requests = []
            errors = []

            def serve():
                try:
                    stream, _ = server.accept()
                    with stream:
                        stream.sendall(b'{"QMP":{}}\r\n')
                        reader = stream.makefile("rb")
                        for index in range(3):
                            request = json.loads(reader.readline())
                            requests.append(request)
                            stream.sendall(b'{"event":"STOP"}\r\n{"id":999,"return":{}}\r\n')
                            response = {"id": request["id"], "return": {"running": True}}
                            if index == 2:
                                response = {"id": request["id"], "error": {"class":"GenericError", "desc":"bad device"}}
                            stream.sendall(json.dumps(response).encode() + b"\r\n")
                except Exception as exc:
                    errors.append(exc)
                finally:
                    server.close()

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                with QMPClient(path) as client:
                    self.assertEqual(client.execute("query-status"), {"running": True})
                    with self.assertRaisesRegex(RuntimeError, "bad device"):
                        client.execute("device_add", {"driver": "bad"})
            finally:
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(requests[0]["execute"], "qmp_capabilities")
            self.assertEqual(requests[2]["arguments"], {"driver": "bad"})

    def test_silent_peer_has_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qmp.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            finished = threading.Event()

            def serve():
                stream, _ = server.accept()
                with stream:
                    finished.wait(2)
                server.close()

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                with self.assertRaises(TimeoutError):
                    QMPClient(path, timeout=0.1).connect()
            finally:
                finished.set()
                worker.join(3)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.script = self.root / "fake_qemu.py"
        self.script.write_text(FAKE_QEMU)
        self.commands = self.root / "commands.jsonl"
        self.supervisor = ProcessSupervisor()
        self.addCleanup(lambda: self.supervisor.stop_all(0))

    def start(self, node_id, mode="normal", startup_timeout=2):
        qmp = self.root / f"qmp-{node_id}.sock"
        self.supervisor.start(node_id, [sys.executable, str(self.script), str(qmp),
                              str(self.commands), mode, str(node_id)], self.root,
                              qmp, self.root / f"node-{node_id}.log", startup_timeout)

    def records(self):
        return [json.loads(line) for line in self.commands.read_text().splitlines()]

    def test_failed_start_does_not_stop_other_node_and_can_restart(self):
        self.start(1)
        with self.assertRaisesRegex(RuntimeError, "node 2 failed"):
            self.start(2, "fail")
        states = self.supervisor.status()
        self.assertEqual(states["1"]["state"], "running")
        self.assertEqual(states["2"]["exit_code"], 7)
        self.assertIn("invalid machine", (self.root / "node-2.log").read_text())
        self.start(2)
        self.assertEqual(self.supervisor.status()["2"]["state"], "running")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.start(2)

    def test_graceful_shutdown_closes_log_and_reaps_child(self):
        self.start(1, "graceful")
        self.supervisor.stop(1, grace_seconds=0.5)
        child = self.supervisor.children[1]
        self.assertEqual(self.supervisor.status()["1"]["state"], "stopped")
        self.assertEqual(child.process.returncode, 0)
        self.assertTrue(child.log.closed)
        self.assertIn("system_powerdown", [record[1] for record in self.records()])
        self.assertNotIn("quit", [record[1] for record in self.records()])

    def test_all_nodes_share_grace_period_and_receive_powerdown_before_quit(self):
        for node_id in range(1, 4):
            self.start(node_id)
        started = time.monotonic()
        self.supervisor.stop_all(grace_seconds=0.2)
        self.assertLess(time.monotonic() - started, 1.5)
        records = self.records()
        powerdowns = [record[2] for record in records if record[1] == "system_powerdown"]
        quits = [record[2] for record in records if record[1] == "quit"]
        self.assertEqual(len(powerdowns), 3)
        self.assertEqual(len(quits), 3)
        self.assertLess(max(powerdowns), min(quits))
        self.assertTrue(all(item["exit_code"] == 0 for item in self.supervisor.status().values()))

    def test_uncooperative_child_is_killed_without_affecting_other_child(self):
        self.start(1, "stubborn")
        self.start(2)
        self.supervisor.stop(1, grace_seconds=0)
        self.assertLess(self.supervisor.status()["1"]["exit_code"], 0)
        self.assertEqual(self.supervisor.status()["2"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
