"""Persistent qcow2 overlays; supplied directories are the storage interface.

Linked-clone disk preparation follows UNetLab cli.php. Copyright (c) 2016,
Andrea Dainese; see LICENSE. No Ceph configuration or local database is used.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from .model import Lab, Node, SpecificationError


DISK_NAME = re.compile(r"(?:(hd|virtide|virtio|scsi|lsi|megasas)([a-z])|disk)\.qcow2\Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise SpecificationError(f"{path} escapes supplied directory {root}")
    return resolved


def image_files(lab: Lab, node: Node) -> list[Path]:
    directory = inside(lab.ticket.templates / node.image, lab.ticket.templates)
    if not directory.is_dir():
        raise SpecificationError(f"node {node.id}: missing template directory {directory}")
    files: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.suffix == ".qcow2":
            if not DISK_NAME.fullmatch(path.name):
                raise SpecificationError(f"node {node.id}: unsupported disk filename {path.name!r}")
            inside(path, lab.ticket.templates)
            if not path.is_file():
                raise SpecificationError(f"node {node.id}: template disk is not a file")
            files.append(path)
    if not files:
        raise SpecificationError(f"node {node.id}: image contains no qcow2 disks")
    cdrom = directory / "cdrom.iso"
    if cdrom.exists():
        inside(cdrom, lab.ticket.templates)
        if not cdrom.is_file():
            raise SpecificationError("cdrom.iso is not a file")
    return files


class AttemptStorage:
    def __init__(self, lab: Lab, *, qemu_img: str = "qemu-img", run: Callable = subprocess.run):
        self.lab = lab
        self.root = lab.ticket.progress
        self.qemu_img = qemu_img
        self.run = run
        self._lock = None

    def acquire(self) -> None:
        """Local/shared filesystem lock is a guard; manager fencing is required."""
        templates = self.lab.ticket.templates.resolve()
        progress = self.root.resolve()
        if progress.is_relative_to(templates) or templates.is_relative_to(progress):
            raise SpecificationError("templates and progress directories must not overlap")
        # Validate every immutable source before touching progress.
        for node in self.lab.nodes:
            image_files(self.lab, node)
        self.root.mkdir(parents=True, exist_ok=True)
        path = inside(self.root / ".puskalka.lock", self.root)
        self._lock = path.open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError("progress directory is already owned by another runner") from exc
        expected = {"attempt_id": self.lab.ticket.attempt_id, "lab_id": self.lab.ticket.lab_id,
                    "lab_version": self.lab.ticket.lab_version,
                    "definition_hash": self.lab.definition_hash}
        marker = inside(self.root / ".puskalka.json", self.root)
        try:
            if marker.exists():
                if json.loads(marker.read_text()) != expected:
                    raise SpecificationError("progress belongs to another attempt or lab definition; refusing to change it")
            else:
                self._atomic_json(marker, expected)
        except Exception:
            self.release()
            raise

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def node_dir(self, node_id: int) -> Path:
        return inside(self.root / "nodes" / str(node_id), self.root)

    def _info(self, path: Path) -> dict:
        result = self.run([self.qemu_img, "info", "--output=json", "-f", "qcow2", str(path)],
                          check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def prepare(self, node: Node) -> list[Path]:
        if self._lock is None:
            raise RuntimeError("acquire attempt ownership before preparing disks")
        target = self.node_dir(node.id)
        target.mkdir(parents=True, exist_ok=True)
        overlays: list[Path] = []
        for base in image_files(self.lab, node):
            overlay = inside(target / base.name, self.root)
            if overlay.exists():
                info = self._info(overlay)
                if info.get("format") != "qcow2":
                    raise SpecificationError(f"{overlay}: expected a qcow2 overlay")
                # Retain the exact stable backing pathname, not just its inode.
                if info.get("backing-filename") != str(base):
                    raise SpecificationError(f"{overlay}: backing file differs from ticket; refusing to overwrite progress")
            else:
                temporary = target / f".{base.name}.{uuid.uuid4().hex}.tmp"
                try:
                    self.run([self.qemu_img, "create", "-f", "qcow2", "-F", "qcow2",
                              "-b", str(base), str(temporary)], check=True, capture_output=True, text=True)
                    with temporary.open("rb") as handle:
                        os.fsync(handle.fileno())
                    temporary.replace(overlay)
                finally:
                    temporary.unlink(missing_ok=True)
            overlays.append(overlay)
        return overlays

    def release(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None
