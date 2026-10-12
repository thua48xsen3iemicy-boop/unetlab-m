"""Build explicit QEMU argv from the UNetLab node, NIC and disk conventions.

Derived model: UNetLab __node.php, copyright (c) 2016 Andrea Dainese, BSD
license retained in LICENSE. Modern host supervision uses QMP, not port scans.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .model import Lab, Node, SpecificationError
from .network import tap_name
from .storage import DISK_NAME, image_files


def escape(value: str | Path) -> str:
    return str(value).replace(",", ",,")


def normalize_options(options: list[str]) -> list[str]:
    """Retain guest hardware choices, replacing obsolete console/wrapper flags."""
    result: list[str] = []
    index = 0
    managed = {"-daemonize", "-qmp", "-pidfile", "-net", "-netdev", "-nic", "-drive",
               "-blockdev", "-hda", "-hdb", "-hdc", "-hdd", "-cdrom", "-smp", "-m",
               "-uuid", "-name", "-vnc", "-accel", "-enable-kvm", "-snapshot"}
    while index < len(options):
        option = options[index]
        if option == "-icount":
            raise SpecificationError("legacy -icount requires TCG; this executor requires KVM. Supply compatible qemu_args.")
        if option in managed:
            raise SpecificationError(f"qemu_args contains runner-owned option {option}")
        if option in {"-serial", "-monitor", "-display", "-realtime"}:
            if index + 1 >= len(options):
                raise SpecificationError(f"{option} requires a value")
            index += 2
            continue
        if option in {"-nographic", "-nodefconfig"}:
            index += 1
            continue
        if option == "-usbdevice":
            if index + 1 >= len(options) or options[index + 1] != "tablet":
                raise SpecificationError("only legacy -usbdevice tablet can be converted")
            result.extend(["-usb", "-device", "usb-tablet"])
            index += 2
            continue
        if option in {"-machine", "-M"}:
            if index + 1 >= len(options):
                raise SpecificationError("-machine requires a value")
            value = ",".join(p for p in options[index + 1].split(",") if not p.startswith("accel="))
            result.extend(["-machine", value])
            index += 2
            continue
        result.append(option)
        index += 1
    return result


def mac_address(lab: Lab, node: Node, interface_id: int) -> str:
    if node.firstmac:
        first = int(node.firstmac.replace(":", ""), 16)
        value = first + interface_id
        if value >= 2**48:
            raise SpecificationError("firstmac overflows the MAC address range")
        octets = value.to_bytes(6, "big")
    elif node.template == "bigip":
        # BIG-IP requires a contiguous MAC range even without explicit firstmac.
        digest = hashlib.sha256(f"{lab.ticket.attempt_id}:{node.id}".encode()).digest()
        octets = bytes([0x02]) + digest[:4] + bytes([interface_id])
    else:
        digest = hashlib.sha256(f"{lab.ticket.attempt_id}:{node.id}:{interface_id}".encode()).digest()
        octets = bytes([0x02]) + digest[:5]
    return ":".join(f"{b:02x}" for b in octets)


def consoles(lab: Lab) -> list[dict]:
    entries: list[dict] = []
    for node in lab.nodes:
        item = {"node_id": node.id, "name": node.name, "bind": lab.ticket.consoles["bind"],
                "protocol": node.console,
                "port": lab.ticket.consoles["vnc_base_port" if node.console == "vnc" else "serial_base_port"] + node.id,
                "serial_port": lab.ticket.consoles["serial_base_port"] + node.id}
        entries.append(item)
    return entries


def disk_args(bases: list[Path], node_dir: Path) -> list[str]:
    args: list[str] = []
    controllers: set[str] = set()
    occupied: set[tuple[str, int]] = set()
    for order, base in enumerate(bases):
        match = DISK_NAME.fullmatch(base.name)
        if not match:
            raise SpecificationError(f"unsupported disk {base.name}")
        kind, suffix = match.groups()
        index = ord(suffix) - ord("a") if suffix else 0
        slot = ("virtio" if kind is None else kind, index)
        if slot in occupied:
            raise SpecificationError(f"duplicate disk slot {slot}: image contains ambiguous disk names")
        occupied.add(slot)
        file = escape(node_dir / base.name)
        if kind == "hd":
            if index > 3:
                raise SpecificationError("IDE disks must be named hda through hdd")
            args += ["-drive", f"file={file},format=qcow2,if=ide,index={index},cache=none"]
        elif kind in {"virtio", None}:
            args += ["-drive", f"file={file},format=qcow2,if=virtio,index={index},cache=none"]
        elif kind == "virtide":
            ident = f"virtide{index}"
            args += ["-drive", f"file={file},format=qcow2,if=none,id={ident},cache=none",
                     "-device", f"virtio-blk-pci,drive={ident},id=hd{suffix},bootindex={order + 1}"]
        elif kind in {"megasas", "lsi", "scsi"}:
            controller = f"scsi-{kind}"
            if controller not in controllers:
                args += ["-device", f"{'megasas' if kind == 'megasas' else 'lsi53c895a'},id={controller}"]
                controllers.add(controller)
            ident = f"disk-{kind}{index}"
            args += ["-drive", f"file={file},format=qcow2,if=none,id={ident},cache=none",
                     "-device", f"scsi-hd,bus={controller}.0,scsi-id={index},drive={ident},bootindex={order + 1}"]
    return args


def command(lab: Lab, node: Node, node_dir: Path, runtime: Path, *,
            bootstrap_args: list[str] | None = None, accelerator: str = "kvm",
            networking: bool = True) -> list[str]:
    # Alternate accelerator is available to explicit integration tests only.
    binary = node.qemu_binary or f"qemu-system-{node.arch}"
    if not isinstance(binary, str) or not binary or "\0" in binary:
        raise SpecificationError("qemu_binary must be an executable path or name")
    args = [binary, *normalize_options(node.qemu_args), "-accel", accelerator,
            "-smp", str(node.cpu), "-m", str(node.ram_mb),
            "-name", f"guest={escape(node.name)}", "-uuid", node.uuid,
            "-qmp", f"unix:{escape(runtime / f'qmp-{node.id}.sock')},server=on,wait=off",
            "-monitor", "none", "-display", "none"]
    serial_port = lab.ticket.consoles["serial_base_port"] + node.id
    args += ["-serial", f"telnet:{lab.ticket.consoles['bind']}:{serial_port},server=on,wait=off"]
    if node.console == "vnc":
        display = lab.ticket.consoles["vnc_base_port"] + node.id - 5900
        args += ["-vnc", f"{lab.ticket.consoles['bind']}:{display},share=force-shared"]
    if networking:
        for interface in node.interfaces:
            net_id = f"net{interface.id}"
            args += ["-netdev", f"tap,id={net_id},ifname={tap_name(node.id, interface.id)},script=no,downscript=no",
                     "-device", f"{node.nic_overrides.get(interface.id, node.nic)},netdev={net_id},mac={mac_address(lab, node, interface.id)}"]
    bases = image_files(lab, node)
    args += disk_args(bases, node_dir)
    if bootstrap_args and node.template in {"vios", "viosl2"} and any(b.name == "virtiob.qcow2" for b in bases):
        raise SpecificationError("vIOS bootstrap disk conflicts with virtiob.qcow2; adjust the image disk layout")
    cdrom = bases[0].parent / "cdrom.iso"
    if cdrom.is_file() and not bootstrap_args:
        args += ["-cdrom", str(cdrom)]
    args += bootstrap_args or []
    return args
