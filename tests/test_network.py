"""Topology and L2-forwarding tests that do not require root privileges."""

import json
import subprocess
import unittest
from types import SimpleNamespace

from puskalka.network import LinuxNetwork, NetworkError, bridge_name, tap_name


class Commands:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.externals = {}
        self.owned_bridges = {}
        self.mask_readback = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail and self.fail(argv):
            raise subprocess.CalledProcessError(1, argv, stderr="command rejected")
        if argv[1:4] == ["link", "add", "name"]:
            self.owned_bridges[argv[4]] = {
                "ifname": argv[4], "mtu": 9000,
                "linkinfo": {"info_kind": "bridge", "info_data": {"group_fwd_mask": 0}},
            }
        if len(argv) > 8 and argv[5:8] == ["type", "bridge", "group_fwd_mask"]:
            value = self.mask_readback if self.mask_readback is not None else int(argv[8])
            self.owned_bridges[argv[4]]["linkinfo"]["info_data"]["group_fwd_mask"] = value
        if argv[1:5] == ["-j", "-d", "link", "show"]:
            name = argv[-1]
            record = self.externals.get(name) or self.owned_bridges.get(name)
            if record is None:
                raise subprocess.CalledProcessError(1, argv, stderr="interface not found")
            return SimpleNamespace(returncode=0, stdout=json.dumps([record]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.commands = Commands()
        self.settings = []

    def write(self, path, value):
        self.settings.append((str(path), value))

    def network(self, networks, ports, **kwargs):
        return LinuxNetwork(networks, ports, run=self.commands, write_sysfs=self.write, **kwargs)

    def test_all_segments_precede_taps_and_unlinked_nics_remain_unlinked(self):
        network = self.network(
            [{"id": 1}, {"id": 2}],
            [
                {"node_id": 1, "id": 0, "network_id": 1},
                {"node_id": 1, "id": 1, "network_id": 0},
                {"node_id": 2, "id": 0, "network_id": 1},
                {"node_id": 2, "id": 1, "network_id": 2},
            ],
        )
        network.prepare()
        calls = self.commands.calls
        bridge_creation = [i for i, argv in enumerate(calls) if argv[1:3] == ["link", "add"]]
        tap_creation = [i for i, argv in enumerate(calls) if argv[1:3] == ["tuntap", "add"]]
        self.assertLess(max(bridge_creation), min(tap_creation))
        for name in ["labbr1", "labbr2", *network.taps.values()]:
            self.assertIn(["ip", "link", "set", "dev", name, "mtu", "9000"], calls)
        self.assertIn(["ip", "link", "set", "dev", "labtap1_0", "master", "labbr1"], calls)
        self.assertIn(["ip", "link", "set", "dev", "labtap2_1", "master", "labbr2"], calls)
        self.assertFalse(any("labtap1_1" in argv and "master" in argv for argv in calls))
        self.assertEqual(network.bridge_for(2), "labbr2")
        call_count = len(calls)
        network.prepare()
        self.assertEqual(len(calls), call_count)
        network.cleanup()
        removals = [argv[-1] for argv in calls if argv[1:3] == ["link", "delete"]]
        self.assertEqual(removals, ["labtap2_1", "labtap2_0", "labtap1_1", "labtap1_0", "labbr2", "labbr1"])

    def test_preserves_full_l2_forwarding_and_short_link_settings(self):
        ports = [{"node_id": node, "id": 0, "network_id": 2} for node in [1, 2, 3]]
        ports.append({"node_id": 4, "id": 0, "network_id": 1})
        network = self.network([{"id": 1}, {"id": 2}], ports)
        network.prepare()
        settings = dict(self.settings)
        for name in ["labbr1", "labbr2"]:
            self.assertEqual(settings[f"/sys/class/net/{name}/bridge/group_fwd_mask"], "65535")
            self.assertEqual(settings[f"/sys/class/net/{name}/bridge/multicast_snooping"], "0")
        self.assertEqual(settings["/sys/class/net/labbr1/bridge/ageing_time"], "0")
        self.assertEqual(settings["/sys/class/net/labbr1/bridge/multicast_router"], "2")
        self.assertNotIn("/sys/class/net/labbr2/bridge/ageing_time", settings)

    def test_restricted_kernel_is_explicit_startup_failure_with_rollback(self):
        def restricted_write(path, value):
            if path.name == "group_fwd_mask":
                raise OSError("Invalid argument")

        network = LinuxNetwork(
            [{"id": 1}], [{"node_id": 1, "id": 0, "network_id": 1}],
            run=self.commands, write_sysfs=restricted_write,
        )
        with self.assertRaisesRegex(NetworkError, r"host kernel.*STP/BPDU.*UNetLab"):
            network.prepare()
        self.assertEqual(self.commands.calls[-1], ["ip", "link", "delete", "dev", "labbr1"])
        self.assertFalse(any(argv[1:3] == ["tuntap", "add"] for argv in self.commands.calls))
        self.assertEqual(network._owned, [])

    def test_default_backend_uses_netlink_with_full_mask_readback(self):
        network = LinuxNetwork(
            [{"id": 1}], [{"node_id": 1, "id": 0, "network_id": 1}],
            run=self.commands,
        )
        network.prepare()
        for option, value in [
            ("group_fwd_mask", "65535"), ("mcast_snooping", "0"),
            ("ageing_time", "0"), ("mcast_router", "2"),
        ]:
            self.assertIn(
                ["ip", "link", "set", "dev", "labbr1", "type", "bridge", option, value],
                self.commands.calls,
            )
        self.assertIn(["ip", "-j", "-d", "link", "show", "dev", "labbr1"], self.commands.calls)
        self.assertEqual(self.settings, [])

    def test_netlink_cannot_silently_keep_incomplete_forwarding_mask(self):
        self.commands.mask_readback = 0
        network = LinuxNetwork([{"id": 1}], [], run=self.commands)
        with self.assertRaisesRegex(NetworkError, "cannot enable group_fwd_mask=65535"):
            network.prepare()
        self.assertEqual(self.commands.calls[-1], ["ip", "link", "delete", "dev", "labbr1"])
        self.assertEqual(network._owned, [])

    def test_netlink_restricted_mask_failure_explains_kernel_requirement(self):
        self.commands.fail = lambda argv: "group_fwd_mask" in argv
        network = LinuxNetwork([{"id": 1}], [], run=self.commands)
        with self.assertRaisesRegex(NetworkError, "host kernel must permit forwarding"):
            network.prepare()
        self.assertEqual(self.commands.calls[-1], ["ip", "link", "delete", "dev", "labbr1"])

    def test_mid_topology_failure_cleans_only_created_links(self):
        self.commands.fail = lambda argv: "labtap2_0" in argv and argv[1:3] == ["tuntap", "add"]
        network = self.network(
            [{"id": 1}],
            [{"node_id": node, "id": 0, "network_id": 1} for node in [1, 2]],
        )
        with self.assertRaisesRegex(NetworkError, "command rejected"):
            network.prepare()
        removals = [argv[-1] for argv in self.commands.calls if argv[1:3] == ["link", "delete"]]
        self.assertEqual(removals, ["labtap1_0", "labbr1"])

    def test_external_bridge_must_be_explicit_and_is_never_mutated_or_removed(self):
        self.commands.externals["uplink0"] = {
            "ifname": "uplink0", "mtu": 9000, "linkinfo": {"info_kind": "bridge"},
        }
        network = self.network(
            [{"id": 1, "type": "pnet0"}],
            [{"node_id": 1, "id": 0, "network_id": 1}],
            external_networks={"pnet0": "uplink0"},
        )
        network.prepare()
        network.cleanup()
        self.assertIn(["ip", "link", "set", "dev", "labtap1_0", "master", "uplink0"], self.commands.calls)
        self.assertFalse(any(argv[1:3] in [["link", "set"], ["link", "delete"]] and argv[4] == "uplink0"
                             for argv in self.commands.calls))
        self.assertEqual(self.settings, [])

    def test_external_non_bridge_or_small_mtu_is_rejected_before_ownership(self):
        for linkinfo, mtu in [({"info_kind": "veth"}, 9000), ({"info_kind": "bridge"}, 1500)]:
            with self.subTest(linkinfo=linkinfo, mtu=mtu):
                self.commands.externals["uplink0"] = {"ifname": "uplink0", "mtu": mtu, "linkinfo": linkinfo}
                network = self.network(
                    [{"id": 1, "type": "pnet0"}], [], external_networks={"pnet0": "uplink0"},
                )
                with self.assertRaises(NetworkError):
                    network.prepare()
                self.assertEqual(network._owned, [])

    def test_invalid_topologies_are_rejected_before_execution(self):
        cases = [
            ([{"id": 1, "type": "ovs"}], []),
            ([{"id": 1, "type": "pnet0"}], []),
            ([{"id": 1}, {"id": 1}], []),
            ([{"id": 1}], [{"node_id": 1, "id": 0, "network_id": 2}]),
            ([], [{"node_id": 1, "id": 0}, {"node_id": 1, "id": 0}]),
            ([{"id": True}], []),
        ]
        for networks, ports in cases:
            with self.subTest(networks=networks, ports=ports), self.assertRaises(NetworkError):
                self.network(networks, ports)
        self.assertEqual(self.commands.calls, [])

    def test_cleanup_retains_failures_for_retry(self):
        network = self.network([{"id": 1}], [])
        network.prepare()
        self.commands.fail = lambda argv: argv[1:3] == ["link", "delete"]
        with self.assertRaisesRegex(NetworkError, "network cleanup failed"):
            network.cleanup()
        with self.assertRaisesRegex(NetworkError, "previous network cleanup is incomplete"):
            network.prepare()
        self.commands.fail = None
        network.cleanup()
        self.assertEqual(network._owned, [])

    def test_large_identifiers_produce_stable_short_names(self):
        for name in [bridge_name(10**40), tap_name(10**30, 10**20)]:
            self.assertLessEqual(len(name), 15)
        self.assertEqual(tap_name(10**30, 10**20), tap_name(10**30, 10**20))
        self.assertNotEqual(tap_name(10**30, 10**20), tap_name(10**30, 10**20 + 1))


if __name__ == "__main__":
    unittest.main()
