"""Persistent startup media and the upstream appliance boot conventions."""

import bz2
import subprocess
import tempfile
import unittest
from pathlib import Path

from puskalka.bootstrap import BootstrapError, DEFAULT_ASSETS, prepare_bootstrap


class MediaTools:
    """Record the appliance configuration given to ISO/FAT tooling."""

    def __init__(self):
        self.calls = []
        self.configurations = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "genisoimage":
            source = Path(argv[-1])
            self.configurations.append((source.name, source.read_bytes()))
            Path(argv[argv.index("-o") + 1]).write_bytes(b"test iso:" + source.read_bytes())
        elif argv[0] == "mcopy":
            source = Path(argv[-2])
            self.configurations.append((source.name, source.read_bytes()))
            image = Path(argv[argv.index("-i") + 1])
            if image.read_bytes()[55:63] != b"FAT16   ":
                raise AssertionError("mcopy did not receive the original FAT16 image")
            # Simulate an updated image, without depending on system mtools.
            with image.open("ab") as stream:
                stream.write(source.read_bytes())
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.node_dir = Path(self.temporary.name) / "node"
        self.tools = MediaTools()

    def prepare(self, template, config=b"hostname lab-node\n"):
        return prepare_bootstrap(template, self.node_dir, config, run=self.tools)

    def test_iso_appliance_filenames_and_drives(self):
        cases = {
            "xrv": ("iosxr_config.txt", "-cdrom", None),
            "csr1000v": ("iosxe_config.txt", "-cdrom", None),
            "asav": ("day0-config", "-cdrom", None),
            "titanium": ("nxos_config.txt", "-cdrom", None),
            "vmx": ("juniper.conf", "-drive", "virtio"),
            "vsrx": ("juniper.conf", "-drive", "virtio"),
            "vsrxng": ("juniper.conf", "-drive", "ide"),
            "pfsense": ("config.xml", "-cdrom", None),
        }
        for template, (filename, flag, interface) in cases.items():
            with self.subTest(template=template):
                directory = Path(self.temporary.name) / template
                options = prepare_bootstrap(template, directory, b"initial\n", run=self.tools)
                self.assertEqual(self.tools.configurations[-1], (filename, b"initial\n"))
                self.assertEqual(options[0], flag)
                if interface:
                    self.assertEqual(
                        options[1],
                        f"file={directory / 'config.iso'},if={interface},media=cdrom,index=2",
                    )
                else:
                    self.assertEqual(options[1], str(directory / "config.iso"))
                command, kwargs = self.tools.calls[-1]
                self.assertEqual(command[0], "genisoimage")
                self.assertEqual("-r" in command, template == "asav")
                self.assertTrue(kwargs["check"])
                self.assertTrue((directory / "config.iso").is_file())
                self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_restart_preserves_configuration_and_existing_iso(self):
        first_options = self.prepare("csr1000v", b"first configuration\n")
        startup = self.node_dir / "startup-config"
        iso = self.node_dir / "config.iso"
        source = self.node_dir / "iosxe_config.txt"
        saved = {path: path.read_bytes() for path in (startup, iso, source)}
        self.assertEqual(self.prepare("csr1000v", b"changed ticket config\n"), first_options)
        self.assertEqual(self.prepare("csr1000v", None), first_options)
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_missing_iso_recovers_from_original_saved_config(self):
        self.node_dir.mkdir()
        (self.node_dir / "startup-config").write_bytes(b"already saved\n")
        self.prepare("xrv", b"new config must be ignored\n")
        self.assertEqual(self.tools.configurations, [("iosxr_config.txt", b"already saved\n")])
        self.assertEqual((self.node_dir / "startup-config").read_bytes(), b"already saved\n")

    def test_existing_media_is_attached_without_config(self):
        self.node_dir.mkdir()
        iso = self.node_dir / "config.iso"
        iso.write_bytes(b"existing media")
        self.assertEqual(self.prepare("asav", None), ["-cdrom", str(iso)])
        self.assertEqual(self.tools.calls, [])
        self.assertFalse((self.node_dir / "startup-config").exists())

    def test_vios_fat_image_and_drive_preserve_guest_progress(self):
        original = (DEFAULT_ASSETS / "minidisk.bz2").read_bytes()
        self.assertEqual(len(original), 736)
        self.assertEqual(bz2.decompress(original)[55:63], b"FAT16   ")
        for template in ("vios", "viosl2"):
            with self.subTest(template=template):
                directory = Path(self.temporary.name) / template
                first = prepare_bootstrap(template, directory, b"initial\n", run=self.tools)
                disk = directory / "minidisk"
                self.assertEqual(
                    first,
                    ["-drive", f"file={disk},format=raw,if=virtio,index=1,cache=none"],
                )
                self.assertEqual(self.tools.configurations[-1], ("ios_config.txt", b"initial\n"))
                self.assertEqual(self.tools.calls[-1][0][-1], "::ios_config.txt")
                disk.write_bytes(b"guest saved disk progress")
                count = len(self.tools.calls)
                again = prepare_bootstrap(template, directory, b"new ticket config", run=self.tools)
                self.assertEqual(again, first)
                self.assertEqual(len(self.tools.calls), count)
                self.assertEqual(disk.read_bytes(), b"guest saved disk progress")
                self.assertEqual((directory / "startup-config").read_bytes(), b"initial\n")
        self.assertEqual((DEFAULT_ASSETS / "minidisk.bz2").read_bytes(), original)

    def test_tool_failure_leaves_no_partial_iso_and_retry_uses_saved_config(self):
        def fail(argv, **kwargs):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, argv, stderr=b"ISO creation failed")

        with self.assertRaisesRegex(BootstrapError, "ISO creation failed"):
            prepare_bootstrap("xrv", self.node_dir, b"first config", run=fail)
        self.assertFalse((self.node_dir / "config.iso").exists())
        self.assertEqual(list(self.node_dir.glob(".*.tmp")), [])
        self.prepare("xrv", b"changed config")
        self.assertEqual(self.tools.configurations[-1], ("iosxr_config.txt", b"first config"))

    def test_tool_success_with_no_iso_is_an_error(self):
        def empty_output(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        with self.assertRaisesRegex(BootstrapError, "empty file"):
            prepare_bootstrap("xrv", self.node_dir, b"config", run=empty_output)
        self.assertFalse((self.node_dir / "config.iso").exists())

    def test_missing_fat_asset_fails_without_publishing_disk(self):
        with self.assertRaisesRegex(BootstrapError, "bootstrap FAT image"):
            prepare_bootstrap(
                "vios", self.node_dir, b"config", run=self.tools,
                assets=Path(self.temporary.name) / "missing-assets",
            )
        self.assertFalse((self.node_dir / "minidisk").exists())
        self.assertEqual(list(self.node_dir.glob(".*.tmp")), [])

    def test_unsupported_initial_configuration_is_explicit(self):
        for template in ("veos", "linux", "custom"):
            with self.subTest(template=template):
                directory = Path(self.temporary.name) / template
                with self.assertRaisesRegex(ValueError, "automatic startup configuration is unsupported"):
                    prepare_bootstrap(template, directory, b"initial config", run=self.tools)
                self.assertEqual((directory / "startup-config").read_bytes(), b"initial config")
        self.assertEqual(self.tools.calls, [])

    def test_templates_without_configuration_need_no_media(self):
        for template in ("xrv", "vios", "veos", "linux"):
            for config in (None, b""):
                with self.subTest(template=template, config=config):
                    self.assertEqual(self.prepare(template, config), [])
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(list(self.node_dir.iterdir()), [])

    def test_qemu_drive_escapes_commas_in_directory(self):
        directory = Path(self.temporary.name) / "attempt,1"
        options = prepare_bootstrap("vmx", directory, b"config", run=self.tools)
        self.assertIn("attempt,,1", options[1])


if __name__ == "__main__":
    unittest.main()
