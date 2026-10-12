"""Read a manager's ticket and immutable UNetLab XML without a local database.

The lab/node/interface model follows UNetLab's __lab.php and __node.php.
Copyright (c) 2016, Andrea Dainese; see the retained BSD license in LICENSE.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


class SpecificationError(ValueError):
    """A ticket or lab cannot be executed as specified."""


def integer(value: Any, name: str, minimum: int = 0, maximum: int = 65535) -> int:
    if isinstance(value, bool):
        raise SpecificationError(f"{name} must be an integer")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise SpecificationError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SpecificationError(f"{name} must be a nonempty string without NUL")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SpecificationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise SpecificationError(f"{path}: expected a JSON object")
    return obj


def absolute_path(value: Any, name: str) -> Path:
    path = Path(text(value, name))
    if not path.is_absolute():
        raise SpecificationError(f"{name} must be an absolute container path")
    return path


@dataclass(frozen=True)
class Interface:
    id: int
    network_id: int = 0
    name: str = ""


@dataclass(frozen=True)
class Network:
    id: int
    type: str
    name: str = ""


@dataclass(frozen=True)
class Node:
    id: int
    name: str
    template: str
    image: str
    cpu: int
    ram_mb: int
    ethernet: int
    arch: str
    nic: str
    console: str
    uuid: str
    delay: int
    qemu_args: list[str]
    interfaces: list[Interface]
    config: bytes | None = field(default=None, repr=False)
    firstmac: str | None = None
    qemu_binary: str | None = None
    nic_overrides: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    attempt_id: str
    issued_at: str
    user: dict[str, Any]
    lab_id: str
    lab_version: str
    unl: Path
    templates: Path
    progress: Path
    consoles: dict[str, Any]
    overrides: dict[str, dict[str, Any]]
    external_networks: dict[str, str]


@dataclass(frozen=True)
class Lab:
    ticket: Ticket
    nodes: list[Node]
    networks: list[Network]
    definition_hash: str

    def ports(self) -> list[dict[str, int]]:
        return [dict(node_id=n.id, id=p.id, network_id=p.network_id)
                for n in self.nodes for p in n.interfaces]

    def summary(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket.ticket_id, "attempt_id": self.ticket.attempt_id,
            "lab_id": self.ticket.lab_id, "lab_version": self.ticket.lab_version,
            "definition_hash": self.definition_hash, "node_count": len(self.nodes),
            "resources": {"guest_ram_mb": sum(n.ram_mb for n in self.nodes),
                          "guest_vcpus": sum(n.cpu for n in self.nodes)},
            "networks": [{"id": n.id, "type": n.type, "name": n.name} for n in self.networks],
            "nodes": [{"id": n.id, "name": n.name, "image": n.image,
                       "template": n.template, "cpu": n.cpu, "ram_mb": n.ram_mb,
                       "console": n.console, "ethernet": n.ethernet,
                       "startup_config": n.config is not None} for n in self.nodes],
        }


def load_ticket(path: Path) -> Ticket:
    obj = read_json(path)
    if type(obj.get("schema")) is not int or obj["schema"] != 1:
        raise SpecificationError("unsupported ticket schema; expected schema: 1")
    for section in ("user", "lab", "storage"):
        if not isinstance(obj.get(section), dict):
            raise SpecificationError(f"ticket.{section} must be an object")
    user = obj["user"]
    integer(user.get("id"), "user.id", 1, 2**63 - 1)
    text(user.get("username"), "user.username")
    issued_at = text(obj.get("issued_at"), "issued_at")
    try:
        issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        if issued.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise SpecificationError("issued_at must be an ISO 8601 timestamp with timezone") from exc
    lab_id = text(obj["lab"].get("id"), "lab.id")
    try:
        uuid.UUID(lab_id)
    except ValueError as exc:
        raise SpecificationError("lab.id must be a UUID") from exc
    consoles = obj.get("consoles", {})
    overrides = obj.get("node_overrides", {})
    external = obj.get("external_networks", {})
    for value, name in ((consoles, "consoles"), (overrides, "node_overrides"),
                        (external, "external_networks")):
        if not isinstance(value, dict):
            raise SpecificationError(f"{name} must be an object")
    consoles = {"bind": "0.0.0.0", "vnc_base_port": 5900, "serial_base_port": 4000, **consoles}
    try:
        if ipaddress.ip_address(consoles["bind"]).version != 4:
            raise ValueError("IPv4 required")
    except (ValueError, TypeError) as exc:
        raise SpecificationError("consoles.bind must be an IPv4 address") from exc
    consoles["vnc_base_port"] = integer(consoles["vnc_base_port"], "vnc_base_port", 5900)
    consoles["serial_base_port"] = integer(consoles["serial_base_port"], "serial_base_port", 1024)
    for key, value in overrides.items():
        ident = integer(key, "override node id", 1)
        if key != str(ident):
            raise SpecificationError("node override keys must use canonical decimal IDs")
        if not isinstance(value, dict):
            raise SpecificationError("each node override must be an object")
        allowed = {"image", "cpu", "ram_mb", "ethernet", "arch", "nic", "qemu_args",
                   "qemu_binary", "console", "uuid", "firstmac", "delay", "nic_overrides"}
        if set(value) - allowed:
            raise SpecificationError(f"node {key}: unsupported override fields {sorted(set(value) - allowed)}")
    return Ticket(text(obj.get("ticket_id"), "ticket_id"),
                  text(obj.get("attempt_id"), "attempt_id"), issued_at, user,
                  lab_id, text(obj["lab"].get("version"), "lab.version"),
                  absolute_path(obj["lab"].get("unl"), "lab.unl"),
                  absolute_path(obj["storage"].get("templates"), "storage.templates"),
                  absolute_path(obj["storage"].get("progress"), "storage.progress"),
                  consoles, overrides, external)


def load_lab(ticket: Ticket, profiles: Path) -> Lab:
    try:
        xml = ticket.unl.read_bytes()
        if len(xml) > 8 * 1024 * 1024 or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
            raise SpecificationError("lab XML must be <= 8 MiB and contain no DTD/entities")
        root = ET.fromstring(xml)
    except (OSError, ET.ParseError) as exc:
        raise SpecificationError(f"cannot read lab XML {ticket.unl}: {exc}") from exc
    if root.tag != "lab" or root.get("id") != ticket.lab_id:
        raise SpecificationError("lab XML id must match ticket.lab.id")
    if root.get("version", "1") != ticket.lab_version:
        raise SpecificationError("lab XML version must match ticket.lab.version")
    networks: list[Network] = []
    network_ids: set[int] = set()
    for el in root.findall("./topology/networks/network"):
        ident = integer(el.get("id"), "network.id", 1)
        if ident in network_ids:
            raise SpecificationError(f"duplicate network id {ident}")
        network_ids.add(ident)
        kind = el.get("type", "bridge")
        if kind != "bridge" and not re.fullmatch(r"pnet[0-9]+", kind):
            raise SpecificationError(f"unsupported network type {kind!r}; first iteration supports bridge and mapped pnet")
        if kind != "bridge" and kind not in ticket.external_networks:
            raise SpecificationError(f"external network {kind} needs manager-supplied external_networks mapping")
        networks.append(Network(ident, kind, el.get("name", "")))
    configs: dict[int, bytes] = {}
    for el in root.findall("./objects/configs/config"):
        ident = integer(el.get("id"), "config node id", 1)
        if ident in configs:
            raise SpecificationError(f"duplicate startup config for node {ident}")
        try:
            configs[ident] = base64.b64decode("".join((el.text or "").split()), validate=True)
        except ValueError as exc:
            raise SpecificationError(f"node {ident}: startup config is not base64") from exc
    nodes: list[Node] = []
    node_ids: set[int] = set()
    console_ports: set[int] = set()
    for el in root.findall("./topology/nodes/node"):
        ident = integer(el.get("id"), "node.id", 1)
        if ident in node_ids:
            raise SpecificationError(f"duplicate node id {ident}")
        node_ids.add(ident)
        if el.get("type", "qemu") != "qemu":
            raise SpecificationError(f"node {ident}: only QEMU nodes are supported")
        template = text(el.get("template"), "node.template")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", template):
            raise SpecificationError(f"invalid template name {template!r}")
        profile = read_json(profiles / f"{template}.json")
        override = ticket.overrides.get(str(ident), {})
        attrs = {**profile, **el.attrib, **override}
        # UNL calls memory 'ram'; the execution ticket consistently uses MiB.
        ram = override.get("ram_mb", el.get("ram", profile.get("ram_mb")))
        ethernet = integer(attrs.get("ethernet", 1), "node.ethernet", 0, 128)
        image = text(attrs.get("image"), "node.image")
        image_parts = Path(image).parts
        if Path(image).is_absolute() or not image_parts or any(p in (".", "..") for p in image.split("/")):
            raise SpecificationError("node.image must be a relative immutable image directory")
        interface_map: dict[int, Interface] = {}
        for port in el.findall("interface"):
            if port.get("type", "ethernet") != "ethernet":
                raise SpecificationError(f"node {ident}: serial topology links are unsupported")
            iface = integer(port.get("id"), "interface.id", 0, 127)
            if iface >= ethernet or iface in interface_map:
                raise SpecificationError(f"node {ident}: duplicate or out-of-range interface {iface}")
            segment = integer(port.get("network_id", "0"), "interface.network_id")
            if segment and segment not in network_ids:
                raise SpecificationError(f"node {ident} interface {iface}: unknown network {segment}")
            interface_map[iface] = Interface(iface, segment, port.get("name", f"eth{iface}"))
        interfaces = [interface_map.get(i, Interface(i, 0, f"eth{i}")) for i in range(ethernet)]
        arch = attrs.get("arch", "x86_64")
        if arch not in ("i386", "x86_64"):
            raise SpecificationError(f"node {ident}: unsupported architecture {arch!r}")
        nic = text(attrs.get("nic", "e1000"), "node.nic")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", nic):
            raise SpecificationError("invalid QEMU NIC model")
        nic_overrides: dict[int, str] = {}
        if not isinstance(attrs.get("nic_overrides", {}), dict):
            raise SpecificationError("nic_overrides must be an object")
        for iface, model in attrs.get("nic_overrides", {}).items():
            port = integer(iface, "nic_overrides interface", 0, 127)
            if port >= ethernet or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", model):
                raise SpecificationError("invalid nic_overrides interface or model")
            nic_overrides[port] = model
        console = attrs.get("console", "vnc")
        if console not in ("vnc", "telnet"):
            raise SpecificationError("node.console must be vnc or telnet")
        for port in [ticket.consoles["serial_base_port"] + ident] + (
                [ticket.consoles["vnc_base_port"] + ident] if console == "vnc" else []):
            if port > 65535 or port in console_ports:
                raise SpecificationError(f"node {ident}: console port out of range or conflicting")
            console_ports.add(port)
        node_uuid = attrs.get("uuid") or str(uuid.uuid5(uuid.UUID(ticket.lab_id), str(ident)))
        try:
            uuid.UUID(node_uuid)
        except (ValueError, TypeError) as exc:
            raise SpecificationError(f"node {ident}: invalid uuid") from exc
        args = attrs.get("qemu_args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise SpecificationError("qemu_args must be an array of strings")
        firstmac = attrs.get("firstmac")
        if firstmac is not None and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", firstmac):
            raise SpecificationError(f"node {ident}: invalid firstmac")
        config_enabled = str(attrs.get("config", "0")) in ("1", "Exported")
        nodes.append(Node(ident, text(attrs.get("name", f"node-{ident}"), "node.name"),
                          template, image, integer(attrs.get("cpu", 1), "node.cpu", 1, 1024),
                          integer(ram, "node.ram_mb", 1, 2**31 - 1), ethernet, arch, nic,
                          console, node_uuid, integer(attrs.get("delay", 0), "node.delay", 0, 3600),
                          args, interfaces, configs.get(ident) if config_enabled else None,
                          firstmac, attrs.get("qemu_binary"), nic_overrides))
    if not nodes:
        raise SpecificationError("a lab must contain at least one QEMU node")
    if set(ticket.overrides) - {str(n) for n in node_ids}:
        raise SpecificationError("node_overrides references a missing node")
    if set(configs) - node_ids:
        raise SpecificationError("startup config references a missing node")
    # Include effective profiles/overrides in identity without persisting the XML.
    effective = [{**n.__dict__, "config": n.config.hex() if n.config else None,
                  "interfaces": [p.__dict__ for p in n.interfaces]} for n in nodes]
    digest = hashlib.sha256(xml + json.dumps(effective, sort_keys=True).encode()).hexdigest()
    return Lab(ticket, nodes, networks, digest)
