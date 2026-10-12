"""Build one lab's Ethernet topology inside its container network namespace.

The Linux bridge settings retain the UNetLab behaviour in ``cli.php``. In
particular, a lab link must carry reserved L2 frames such as STP and LACP.
Vanilla Linux kernels restrict ``group_fwd_mask``; failing to enable it is a
startup error, rather than a lab that silently loses those frames. The upstream
kernel patch is retained under ``patch/`` as a reference for host provisioning.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class NetworkError(RuntimeError):
    """The requested lab network could not be built or removed safely."""


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NetworkError(f"{field} must be an integer >= {minimum}")
    return value


def bridge_name(network_id: int) -> str:
    """Return a deterministic Linux interface name, within IFNAMSIZ."""
    network_id = _integer(network_id, "network_id", 1)
    name = f"labbr{network_id}"
    if len(name) > 15:
        name = "labbr" + hashlib.sha256(str(network_id).encode()).hexdigest()[:10]
    return name


def tap_name(node_id: int, interface_id: int) -> str:
    node_id = _integer(node_id, "node_id", 1)
    interface_id = _integer(interface_id, "interface_id")
    name = f"labtap{node_id}_{interface_id}"
    if len(name) > 15:
        digest = hashlib.sha256(f"{node_id}:{interface_id}".encode()).hexdigest()
        name = "lt" + digest[:13]
    return name


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _write_sysfs(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="ascii")
    # Sysfs can display numeric values in hexadecimal. Confirm the kernel kept
    # the requested setting, including the complete forwarding bitmask.
    actual = path.read_text(encoding="ascii").strip()
    if int(actual, 0) != int(value, 0):
        raise OSError(f"kernel retained {actual!r} instead of {value!r}")


class LinuxNetwork:
    """Own bridges and TAPs for a complete lab, never inherited interfaces.

    ``networks`` contain ``id`` and ``type`` (default ``bridge``). ``ports``
    contain ``node_id``, interface ``id``, and ``network_id``; zero means an
    unconnected NIC, for which a TAP is still created. Mappings or dataclasses
    can be supplied. All NICs, including implicit NICs from a node's Ethernet
    count, must already be expanded by the ticket parser.

    An external network such as ``pnet0`` is supported only when the manager
    supplies a mapping to an existing Linux bridge in this namespace. No host
    namespace, interface, bridge, or routing configuration is inherited by
    assumption. OVS requires a separate backend and is rejected explicitly.
    """

    def __init__(
        self,
        networks: Sequence[Any],
        ports: Sequence[Any],
        *,
        external_networks: Mapping[str, str] | None = None,
        run: Callable[..., Any] = subprocess.run,
        write_sysfs: Callable[[Path, str], None] | None = None,
        sysfs_root: str | Path = "/sys/class/net",
    ) -> None:
        self._run = run
        self._write_sysfs = write_sysfs
        self._sysfs_root = Path(sysfs_root)
        self._owned: list[tuple[str, str]] = []
        self._prepared = False
        self._network_types: dict[int, str] = {}
        self.bridges: dict[int, str] = {}
        self.taps: dict[tuple[int, int], str] = {}
        self._ports: list[tuple[int, int, int]] = []
        self._external_bridges: set[str] = set()
        external_networks = dict(external_networks or {})
        names: set[str] = set()

        for network in networks:
            network_id = _integer(_get(network, "id"), "network.id", 1)
            network_type = _get(network, "type", "bridge")
            if network_id in self.bridges:
                raise NetworkError(f"duplicate network id: {network_id}")
            if network_type == "bridge":
                name = bridge_name(network_id)
                if name in names:
                    raise NetworkError(f"network interface name collision: {name}")
            elif isinstance(network_type, str) and re.fullmatch(r"pnet[0-9]+", network_type):
                name = external_networks.get(network_type)
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", name):
                    raise NetworkError(
                        f"{network_type} requires an explicit external_networks mapping "
                        "to a Linux bridge inside the container"
                    )
                if name == "lo":
                    raise NetworkError("loopback cannot be an external lab bridge")
                self._external_bridges.add(name)
            else:
                raise NetworkError(f"unsupported network type {network_type!r}; only bridge and mapped pnet are supported")
            self.bridges[network_id] = name
            self._network_types[network_id] = network_type
            names.add(name)

        internal_names = {
            self.bridges[network_id]
            for network_id, network_type in self._network_types.items()
            if network_type == "bridge"
        }
        if internal_names & self._external_bridges:
            raise NetworkError("an inherited bridge name conflicts with an owned lab bridge")

        for port in ports:
            node_id = _integer(_get(port, "node_id"), "port.node_id", 1)
            interface_id = _integer(_get(port, "id"), "port.id")
            network_id = _integer(_get(port, "network_id", 0), "port.network_id")
            key = (node_id, interface_id)
            if key in self.taps:
                raise NetworkError(f"duplicate port: node {node_id}, interface {interface_id}")
            if network_id and network_id not in self.bridges:
                raise NetworkError(f"port {key} references unknown network {network_id}")
            name = tap_name(node_id, interface_id)
            if name in names:
                raise NetworkError(f"network interface name collision: {name}")
            self.taps[key] = name
            self._ports.append((node_id, interface_id, network_id))
            names.add(name)

    def bridge_for(self, network_id: int) -> str:
        try:
            return self.bridges[network_id]
        except KeyError as exc:
            raise NetworkError(f"unknown network {network_id}") from exc

    def _ip(self, *arguments: str) -> Any:
        argv = ["ip", *arguments]
        try:
            result = self._run(argv, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise NetworkError(f"{' '.join(argv)} failed: {detail.strip()}") from exc
        # Keep injected command runners honest, too: an unsuccessful command
        # cannot grant ownership of an existing interface.
        if getattr(result, "returncode", 0):
            raise NetworkError(f"{' '.join(argv)} failed: {getattr(result, 'stderr', '')}")
        return result

    def _bridge_setting(self, name: str, setting: str, value: str) -> None:
        try:
            if self._write_sysfs is not None:
                # An injectable writer keeps configuration testable and permits
                # environments with explicitly writable namespace-local sysfs.
                self._write_sysfs(self._sysfs_root / name / "bridge" / setting, value)
            else:
                # Docker's /sys is read-only by default. Netlink configures the
                # same kernel bridge properties with NET_ADMIN alone; it does
                # not need a host sysfs bind mount or extra SYS_ADMIN privilege.
                option = {
                    "group_fwd_mask": "group_fwd_mask",
                    "multicast_snooping": "mcast_snooping",
                    "ageing_time": "ageing_time",
                    "multicast_router": "mcast_router",
                }[setting]
                self._ip("link", "set", "dev", name, "type", "bridge", option, value)
                if setting == "group_fwd_mask":
                    result = self._ip("-j", "-d", "link", "show", "dev", name)
                    records = json.loads(result.stdout)
                    actual = records[0]["linkinfo"]["info_data"]["group_fwd_mask"]
                    actual_mask = int(actual, 0) if isinstance(actual, str) else actual
                    if isinstance(actual_mask, bool) or actual_mask != 65535:
                        raise ValueError(f"kernel retained group_fwd_mask={actual!r}")
        except (OSError, ValueError, KeyError, IndexError, AttributeError, TypeError, NetworkError) as exc:
            if setting == "group_fwd_mask":
                raise NetworkError(
                    f"bridge {name}: cannot enable group_fwd_mask=65535. "
                    "The host kernel must permit forwarding all reserved L2 frames "
                    "(STP/BPDU, LLDP and LACP), matching the UNetLab bridge kernel patch. "
                    "Standard kernels restrict this mask; container privileges cannot fix it. "
                    f"Kernel response: {exc}"
                ) from exc
            raise NetworkError(f"bridge {name}: cannot set {setting}={value}: {exc}") from exc

    def _verify_external(self, name: str) -> None:
        result = self._ip("-j", "-d", "link", "show", "dev", name)
        try:
            records = json.loads(result.stdout)
            record = records[0]
            is_bridge = record["linkinfo"]["info_kind"] == "bridge"
            mtu = record["mtu"]
        except (ValueError, TypeError, IndexError, KeyError, AttributeError) as exc:
            raise NetworkError(f"cannot verify external Linux bridge {name}") from exc
        if len(records) != 1 or record.get("ifname") != name or not is_bridge:
            raise NetworkError(f"external interface {name} is not a Linux bridge")
        if not isinstance(mtu, int) or mtu < 9000:
            raise NetworkError(f"external bridge {name} must already have MTU >= 9000")

    def prepare(self) -> None:
        """Create the entire topology, rolling back owned links on failure."""
        if self._prepared:
            return
        if self._owned:
            raise NetworkError("previous network cleanup is incomplete; retry cleanup before prepare")
        counts = Counter(network_id for _, _, network_id in self._ports if network_id)
        try:
            for name in sorted(self._external_bridges):
                self._verify_external(name)

            # Finish all segments before creating any node's network device.
            for network_id, network_type in self._network_types.items():
                if network_type != "bridge":
                    continue
                name = self.bridges[network_id]
                self._ip("link", "add", "name", name, "type", "bridge")
                self._owned.append(("bridge", name))
                self._ip("link", "set", "dev", name, "mtu", "9000")
                self._bridge_setting(name, "group_fwd_mask", "65535")
                self._bridge_setting(name, "multicast_snooping", "0")
                if counts[network_id] <= 2:
                    self._bridge_setting(name, "ageing_time", "0")
                    self._bridge_setting(name, "multicast_router", "2")
                self._ip("link", "set", "dev", name, "up")

            for node_id, interface_id, network_id in self._ports:
                name = self.taps[(node_id, interface_id)]
                self._ip("tuntap", "add", "dev", name, "mode", "tap")
                self._owned.append(("tap", name))
                self._ip("link", "set", "dev", name, "mtu", "9000")
                if network_id:
                    self._ip("link", "set", "dev", name, "master", self.bridges[network_id])
                self._ip("link", "set", "dev", name, "up")
            self._prepared = True
        except Exception as exc:
            try:
                self.cleanup()
            except NetworkError as cleanup_error:
                raise NetworkError(f"{exc}; rollback incomplete: {cleanup_error}") from exc
            raise

    def cleanup(self) -> None:
        """Remove only links created here, TAPs before their lab bridges.

        Failed removals retain ownership so that callers can retry cleanup.
        QEMU processes must be stopped by the caller before invoking this.
        """
        failed: list[tuple[str, str]] = []
        errors: list[str] = []
        for kind, name in reversed(self._owned):
            try:
                self._ip("link", "delete", "dev", name)
            except NetworkError as exc:
                failed.append((kind, name))
                errors.append(str(exc))
        self._owned = list(reversed(failed))
        self._prepared = False
        if errors:
            raise NetworkError("network cleanup failed: " + "; ".join(errors))
