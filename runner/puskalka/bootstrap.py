"""Prepare optional startup media without replacing a node's saved progress.

The template filenames and boot-media choices are adapted from UNetLab's
``html/includes/cli.php`` and ``scripts/createdosdisk.sh`` (Andrea Dainese,
2014-2016, BSD-3-Clause; see the repository LICENSE). The original FAT image
is retained in ``assets/minidisk.bz2``. Using mtools instead of mounting that
image removes the bootstrap step's need for loop devices or mount privileges.

Only initial configuration injection is handled here. Node directories belong
to an attempt and survive container restarts; existing configuration and media
always win over a newly supplied configuration.
"""

from __future__ import annotations

import bz2
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


DEFAULT_ASSETS = Path(__file__).resolve().parents[2] / "assets"
ISO_CONFIG_FILES = {
    "xrv": "iosxr_config.txt",
    "csr1000v": "iosxe_config.txt",
    "asav": "day0-config",
    "titanium": "nxos_config.txt",
    "vmx": "juniper.conf",
    "vsrx": "juniper.conf",
    "vsrxng": "juniper.conf",
    "pfsense": "config.xml",
}
FAT_TEMPLATES = frozenset({"vios", "viosl2"})


class BootstrapError(RuntimeError):
    """A supported startup medium could not be prepared."""


def _publish_missing(temporary: Path, destination: Path) -> None:
    """Publish a complete file atomically, never replacing an existing file."""
    # Both files are on the same attempt filesystem. link(), unlike replace(),
    # preserves the first published file even if another caller appears.
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if not destination.is_file():
            raise BootstrapError(f"bootstrap destination is not a file: {destination}")


def _write_missing(destination: Path, content: bytes) -> None:
    if destination.is_file():
        return
    temporary = _temporary(destination)
    try:
        temporary.write_bytes(content)
        _publish_missing(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary(destination: Path) -> Path:
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    return Path(name)


def _invoke(run: Callable[..., Any], argv: list[str]) -> None:
    try:
        run(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        suffix = f": {detail.strip()}" if detail else f": {exc}"
        raise BootstrapError(f"startup media command {argv[0]} failed{suffix}") from exc


def _drive_path(path: Path) -> str:
    # QEMU parses comma-separated drive properties independently of argv.
    return str(path).replace(",", ",,")


def _media_options(template: str, media: Path) -> list[str]:
    if template in FAT_TEMPLATES:
        return [
            "-drive",
            f"file={_drive_path(media)},format=raw,if=virtio,index=1,cache=none",
        ]
    if template in {"vmx", "vsrx", "vsrxng"}:
        interface = "ide" if template == "vsrxng" else "virtio"
        return [
            "-drive",
            f"file={_drive_path(media)},if={interface},media=cdrom,index=2",
        ]
    return ["-cdrom", str(media)]


def prepare_bootstrap(
    node_template: str,
    node_dir: Path,
    config: bytes | None,
    *,
    run: Callable[..., Any] = subprocess.run,
    assets: Path = DEFAULT_ASSETS,
) -> list[str]:
    """Return extra QEMU arguments for this node's persistent startup medium.

    ``config`` is the decoded startup configuration, or ``None`` when the
    ticket has none. Empty configuration matches upstream's no-config case.
    Missing media can also be recovered from an existing ``startup-config``.
    Once written, these files are preserved on every restart, even when the
    manager supplies different configuration bytes.

    An unsupported template with configuration raises ``ValueError`` rather
    than silently starting a guest whose initial configuration was ignored.
    In particular, the old vEOS disk-modification/pexpect workflow is outside
    this initial implementation. Templates without configuration need no
    bootstrap support and return an empty argument list.
    """
    if not isinstance(node_template, str) or not node_template:
        raise ValueError("node_template must be a nonempty template name")
    if config is not None and not isinstance(config, bytes):
        raise TypeError("startup configuration must be bytes or None")

    node_dir = Path(node_dir).resolve()
    node_dir.mkdir(parents=True, exist_ok=True)
    startup = node_dir / "startup-config"
    if config:
        _write_missing(startup, config)

    if node_template not in ISO_CONFIG_FILES and node_template not in FAT_TEMPLATES:
        if startup.is_file() and startup.stat().st_size:
            raise ValueError(
                f"automatic startup configuration is unsupported for template "
                f"{node_template!r}; startup-config was preserved but cannot be "
                "injected by this runner"
            )
        return []

    media = node_dir / ("minidisk" if node_template in FAT_TEMPLATES else "config.iso")
    if media.is_file():
        return _media_options(node_template, media)
    if not startup.is_file() or startup.stat().st_size == 0:
        return []

    source_name = (
        "ios_config.txt" if node_template in FAT_TEMPLATES
        else ISO_CONFIG_FILES[node_template]
    )
    source = node_dir / source_name
    _write_missing(source, startup.read_bytes())
    temporary = _temporary(media)
    try:
        if node_template in FAT_TEMPLATES:
            try:
                temporary.write_bytes(
                    bz2.decompress((Path(assets) / "minidisk.bz2").read_bytes())
                )
            except (OSError, EOFError, ValueError) as exc:
                raise BootstrapError(f"cannot read the bootstrap FAT image: {exc}") from exc
            _invoke(run, ["mcopy", "-i", str(temporary), str(source), "::ios_config.txt"])
        else:
            argv = ["genisoimage"]
            if node_template == "asav":
                argv.append("-r")
            argv.extend(["-o", str(temporary), "-l", "--iso-level", "2", str(source)])
            _invoke(run, argv)

        if temporary.stat().st_size == 0:
            raise BootstrapError(f"startup media command produced an empty file for {node_template}")
        _publish_missing(temporary, media)
    finally:
        temporary.unlink(missing_ok=True)

    return _media_options(node_template, media)
