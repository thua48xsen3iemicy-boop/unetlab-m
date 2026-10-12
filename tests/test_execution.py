"""Regression checks for the ticket, immutable disks and real QEMU endpoints."""

import dataclasses
import hashlib
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from puskalka.model import SpecificationError, load_lab, load_ticket
from puskalka.qemu import command, disk_args, normalize_options, mac_address
from puskalka.storage import AttemptStorage


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.templates = self.root / "templates"
        self.image = self.templates / "linux" / "v1"
        self.image.mkdir(parents=True)
        self.base = self.image / "virtioa.qcow2"
        self.base.write_bytes(b"base")
        self.progress = self.root / "progress"
        self.xml = self.root / "lab.unl"
        self.xml.write_text('''<lab id="70bcb98d-5655-4e87-a1b3-266e9287d42d" version="1">
        <topology><nodes><node id="1" name="Linux" type="qemu" template="linux"
        image="linux/v1" cpu="2" ram="128" ethernet="2" console="vnc">
        <interface id="0" type="ethernet" network_id="1"/></node></nodes>
        <networks><network id="1" type="bridge"/></networks></topology></lab>''')
        (self.profiles / "linux.json").write_text(json.dumps({"cpu": 1, "ram_mb": 64,
            "arch": "x86_64", "nic": "virtio-net-pci", "qemu_args": ["-machine", "pc"]}))
        self.ticket_path = self.root / "ticket.json"
        self.data = {"schema": 1, "ticket_id": "ticket-1", "attempt_id": "attempt-1",
            "issued_at": "2026-10-12T09:00:00Z", "user": {"id": 42, "username": "student"},
            "lab": {"id": "70bcb98d-5655-4e87-a1b3-266e9287d42d", "version": "1", "unl": str(self.xml)},
            "storage": {"templates": str(self.templates), "progress": str(self.progress)},
            "consoles": {"bind": "127.0.0.1"}}

    def load(self):
        self.ticket_path.write_text(json.dumps(self.data))
        return load_lab(load_ticket(self.ticket_path), self.profiles)


class SpecificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def test_overrides_win_over_xml_and_profile_and_unlinked_nic_is_retained(self):
        self.f.data["node_overrides"] = {"1": {"cpu": 3, "ram_mb": 256}}
        lab = self.f.load()
        node = lab.nodes[0]
        self.assertEqual((node.cpu, node.ram_mb, node.nic), (3, 256, "virtio-net-pci"))
        self.assertEqual(lab.ports(), [{"node_id": 1, "id": 0, "network_id": 1},
                                     {"node_id": 1, "id": 1, "network_id": 0}])
        self.assertFalse(self.f.progress.exists())

    def test_manager_resource_typo_is_rejected(self):
        self.f.data["node_overrides"] = {"1": {"ram": 256}}
        with self.assertRaisesRegex(SpecificationError, "unsupported override"):
            self.f.load()

    def test_broken_topology_is_rejected_without_side_effects(self):
        self.f.xml.write_text(self.f.xml.read_text().replace('network_id="1"', 'network_id="8"'))
        with self.assertRaisesRegex(SpecificationError, "unknown network"):
            self.f.load()
        self.assertFalse(self.f.progress.exists())

    def test_console_ports_cannot_collide_between_protocols(self):
        self.f.data["consoles"]["serial_base_port"] = 5900
        with self.assertRaisesRegex(SpecificationError, "console port"):
            self.f.load()

    def test_unknown_node_type_rejected(self):
        self.f.xml.write_text(self.f.xml.read_text().replace('type="qemu"', 'type="docker"'))
        with self.assertRaisesRegex(SpecificationError, "only QEMU"):
            self.f.load()

    def test_version_mismatch_rejected(self):
        self.f.data["lab"]["version"] = "2"
        with self.assertRaisesRegex(SpecificationError, "version"):
            self.f.load()

    def test_paths_cannot_escape_templates(self):
        self.f.data["node_overrides"] = {"1": {"image": "../elsewhere"}}
        with self.assertRaisesRegex(SpecificationError, "relative immutable"):
            self.f.load()

    def test_legacy_console_arguments_are_replaced_without_changing_hardware(self):
        args = normalize_options(["-machine", "type=pc-1.0,accel=kvm,usb=off", "-serial", "mon:stdio",
                                  "-nographic", "-nodefconfig", "-rtc", "base=utc"])
        self.assertEqual(args, ["-machine", "type=pc-1.0,usb=off", "-rtc", "base=utc"])

    def test_disk_slot_ambiguity_is_rejected(self):
        with self.assertRaisesRegex(SpecificationError, "duplicate disk slot"):
            disk_args([Path("disk.qcow2"), Path("virtioa.qcow2")], Path("/progress/nodes/1"))

    def test_mixed_controllers_have_unique_bootindices(self):
        args = disk_args([Path("lsia.qcow2"), Path("megasasa.qcow2"), Path("virtidea.qcow2")], Path("/progress"))
        devices = [value for value in args if "bootindex=" in value]
        self.assertEqual(len({value.split("bootindex=")[1] for value in devices}), 3)

    def test_bigip_mac_range_is_contiguous_and_stable(self):
        lab = self.f.load()
        node = dataclasses.replace(lab.nodes[0], template="bigip")
        first = mac_address(lab, node, 0)
        second = mac_address(lab, node, 1)
        self.assertEqual(int(second.replace(":", ""), 16) - int(first.replace(":", ""), 16), 1)
        self.assertEqual(first, mac_address(lab, node, 0))


@unittest.skipUnless(shutil.which("qemu-img") and shutil.which("qemu-system-x86_64"), "real QEMU tools are required")
class QemuIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="puskalka-test-")
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)
        self.f.base.unlink()
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(self.f.base), "8M"], check=True, capture_output=True)

    def test_resume_keeps_overlay_writes_and_template_unchanged(self):
        lab = self.f.load()
        original = hashlib.sha256(self.f.base.read_bytes()).hexdigest()
        storage = AttemptStorage(lab)
        storage.acquire()
        try:
            overlay = storage.prepare(lab.nodes[0])[0]
            subprocess.run(["qemu-io", "-f", "qcow2", "-c", "write -P 0x5a 0 512", str(overlay)],
                           check=True, capture_output=True)
            progress_hash = hashlib.sha256(overlay.read_bytes()).hexdigest()
        finally:
            storage.release()
        resumed = AttemptStorage(lab)
        resumed.acquire()
        try:
            resumed.prepare(lab.nodes[0])
            self.assertEqual(hashlib.sha256(overlay.read_bytes()).hexdigest(), progress_hash)
            self.assertEqual(hashlib.sha256(self.f.base.read_bytes()).hexdigest(), original)
        finally:
            resumed.release()

    def test_changed_attempt_cannot_reuse_another_attempt_directory(self):
        lab = self.f.load()
        storage = AttemptStorage(lab)
        storage.acquire()
        storage.prepare(lab.nodes[0])
        storage.release()
        changed = dataclasses.replace(lab, ticket=dataclasses.replace(lab.ticket, attempt_id="attempt-2"))
        with self.assertRaisesRegex(SpecificationError, "another attempt"):
            AttemptStorage(changed).acquire()

    def test_changed_backing_is_refused(self):
        lab = self.f.load()
        storage = AttemptStorage(lab)
        storage.acquire()
        try:
            directory = storage.node_dir(1)
            directory.mkdir(parents=True)
            wrong = self.f.image / "wrong.qcow2"
            subprocess.run(["qemu-img", "create", "-f", "qcow2", str(wrong), "8M"], check=True, capture_output=True)
            subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(wrong),
                            str(directory / self.f.base.name)], check=True, capture_output=True)
            wrong.unlink()
            with self.assertRaisesRegex(SpecificationError, "backing file differs"):
                storage.prepare(lab.nodes[0])
        finally:
            storage.release()

    def test_real_startup_media_and_secondary_virtio_disk(self):
        from puskalka.bootstrap import prepare_bootstrap
        from puskalka.processes import ProcessSupervisor
        lab = self.f.load()
        storage = AttemptStorage(lab)
        storage.acquire()
        self.addCleanup(storage.release)
        storage.prepare(lab.nodes[0])
        config = b"hostname initial-node\n"
        directory = storage.node_dir(1)
        extra = prepare_bootstrap("viosl2", directory, config)
        result = subprocess.run(["mtype", "-i", str(directory / "minidisk"), "::ios_config.txt"],
                                check=True, capture_output=True)
        self.assertEqual(result.stdout, config)
        iso_dir = self.f.root / "iso"
        prepare_bootstrap("csr1000v", iso_dir, config)
        result = subprocess.run(["isoinfo", "-i", str(iso_dir / "config.iso"), "-x", "/IOSXE_CONFIG.TXT;1"],
                                check=True, capture_output=True)
        self.assertEqual(result.stdout, config)
        runtime = self.f.root / "runtime"
        runtime.mkdir()
        node = dataclasses.replace(lab.nodes[0], template="viosl2", console="telnet")
        lab = dataclasses.replace(lab, nodes=[node])
        supervisor = ProcessSupervisor()
        self.addCleanup(lambda: supervisor.stop_all(grace_seconds=0))
        args = command(lab, node, directory, runtime, bootstrap_args=extra, accelerator="tcg", networking=False)
        supervisor.start(1, args, directory, runtime / "qmp-1.sock", runtime / "qemu.log")
        self.assertEqual(supervisor.status()["1"]["state"], "running")

    def test_real_qemu_accepts_every_supported_disk_controller(self):
        from puskalka.processes import ProcessSupervisor
        for name in ("hda.qcow2", "virtioa.qcow2", "virtidea.qcow2", "scsia.qcow2", "lsia.qcow2", "megasasa.qcow2"):
            with self.subTest(disk=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                disk = directory / name
                subprocess.run(["qemu-img", "create", "-f", "qcow2", str(disk), "8M"], check=True, capture_output=True)
                qmp = directory / "qmp.sock"
                args = ["qemu-system-x86_64", "-machine", "pc", "-accel", "tcg", "-m", "64",
                        "-display", "none", "-monitor", "none", "-serial", "none",
                        "-qmp", f"unix:{qmp},server=on,wait=off", *disk_args([disk], directory)]
                supervisor = ProcessSupervisor()
                try:
                    supervisor.start(1, args, directory, qmp, directory / "qemu.log")
                    self.assertEqual(supervisor.status()["1"]["state"], "running")
                finally:
                    supervisor.stop_all(grace_seconds=0)

    @staticmethod
    def free_port():
        with socket.socket() as stream:
            stream.bind(("127.0.0.1", 0))
            return stream.getsockname()[1]

    @staticmethod
    def recv_exact(stream, count):
        result = b""
        while len(result) < count:
            part = stream.recv(count - len(result))
            if not part:
                raise AssertionError("VNC connection closed")
            result += part
        return result

    def test_real_qmp_vnc_and_keyboard_pointer_input(self):
        from puskalka.processes import ProcessSupervisor, QMPClient
        vnc = self.free_port()
        serial = self.free_port()
        self.f.data["consoles"].update(vnc_base_port=vnc - 1, serial_base_port=serial - 1)
        lab = self.f.load()
        storage = AttemptStorage(lab)
        storage.acquire()
        self.addCleanup(storage.release)
        storage.prepare(lab.nodes[0])
        runtime = self.f.root / "runtime"
        runtime.mkdir()
        supervisor = ProcessSupervisor()
        self.addCleanup(lambda: supervisor.stop_all(grace_seconds=0.05))
        qmp = runtime / "qmp-1.sock"
        args = command(lab, lab.nodes[0], storage.node_dir(1), runtime, accelerator="tcg", networking=False)
        supervisor.start(1, args, storage.node_dir(1), qmp, runtime / "qemu.log")
        with QMPClient(qmp) as client:
            self.assertTrue(client.execute("query-status")["running"])
        with socket.create_connection(("127.0.0.1", vnc), timeout=3) as stream:
            self.assertTrue(self.recv_exact(stream, 12).startswith(b"RFB "))
            stream.sendall(b"RFB 003.008\n")
            count = self.recv_exact(stream, 1)[0]
            self.assertIn(1, self.recv_exact(stream, count))
            stream.sendall(b"\x01")
            self.assertEqual(self.recv_exact(stream, 4), b"\0\0\0\0")
            stream.sendall(b"\x01")
            header = self.recv_exact(stream, 24)
            self.recv_exact(stream, struct.unpack("!I", header[20:24])[0])
            stream.sendall(struct.pack("!BBHI", 4, 1, 0, ord("a")))
            stream.sendall(struct.pack("!BBHI", 4, 0, 0, ord("a")))
            stream.sendall(struct.pack("!BBHH", 5, 0, 10, 10))
        supervisor.stop_all(grace_seconds=0.05)
        self.assertNotEqual(supervisor.status()["1"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
