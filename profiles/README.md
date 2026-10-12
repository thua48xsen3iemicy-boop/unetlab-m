# QEMU profiles

These JSON files retain the QEMU appliance defaults extracted from the
original UNetLab `html/templates/*.php` files. The profiles contain runtime
defaults only; images, network connections, node resources and overrides
come from the trusted manager's ticket and `.unl` lab description.

`linux.json` is a new generic profile for current x86_64 QEMU: 1 vCPU,
1024 MiB RAM, two VirtIO NICs and a VNC console. `linux-legacy.json`
records the recovered original Linux options. Other names match the
original template identifiers.

Fields: `cpu`, `ram_mb`, `ethernet`, `arch`, `nic`, `console`, `qemu_args`.
Optional `nic_overrides` maps interface IDs to appliance-specific NIC models.
`qemu_args` is an argument array, never a shell command.
`source_qemu_version`, where present, records an explicit original version
selection: CIPS used QEMU 1.3.1; Win and NSVPX used 2.0.2. This metadata
does not install or select those obsolete binaries. A manager can supply
an explicit compatible binary where an appliance requires one.

## Compatibility and source recovery

The appliance argument arrays preserve original machine models and options,
including `pc-1.0`, legacy `-usbdevice`, `-nodefconfig`, `-realtime`, console
flags and acceleration choices. The runtime normalizes options it owns or
that current QEMU removed. Other appliance requirements still need validation
against the image and QEMU version chosen for deployment. Imported defaults
do not imply that every proprietary appliance has been boot-tested.

ASA originally selected TCG and `-icount 1`; it is not a validated KVM
profile. TiMOS originally switched CPU and acceleration defaults based on
host virtualization; the imported profile uses its KVM branch, two vCPUs.
VWAAS SMBIOS quoting is represented as one argument so embedded spaces survive
direct process execution.

The original `linux.php`, `bigip.php` and `sonicwall.php` files had incomplete
comment blocks and lacked CPU/RAM/port/console defaults. Their apparent QEMU
literal data was recovered instead of executing the malformed PHP. Missing
CPU, RAM, NIC and console use original Node-class fallback values: 1, 1024,
`e1000`, `telnet`. Missing Ethernet count uses four ports, the original
invalid-count fallback. The manager should specify resources and interface
count for these recovered appliance profiles.

## Runtime behavior outside the template files

The original Node class also contained these appliance rules, so profile
data alone does not fully reproduce every vendor integration:

- A10 interface 0 is always `e1000`; subsequent interfaces use the profile's
  `virtio-net-pci` NIC. Preserve this ordering when attaching its management
  and data networks.
- BIG-IP assigns sequential MAC addresses starting at optional `firstmac`.
  Without it, the runner supplies a stable contiguous local MAC range per attempt.
- Vendor interface labels (`Mgmt`, `Gi0/0`, `fxp0`, etc.) are guest-facing
  labels. `.unl` interface IDs and their network bindings determine the
  actual ordered attachments. vMX reserves interface 1 for internal use;
  interface 0 is management, data interfaces start at 2.
- Disk controller selection originally followed file names, not profiles:
  `hda.qcow2` and siblings for IDE, `virtioa.qcow2` for VirtIO,
  `virtidea.qcow2` for explicit VirtIO block devices, `scsia.qcow2` for SCSI,
  and `lsia.qcow2` / `megasasa.qcow2` for dedicated SCSI controllers.
  `cdrom.iso` was mounted read-only. The runtime owns this disk mapping.

## Attribution and license

The imported profile values are derived from UNetLab by Andrea Dainese.
Its original BSD-3-Clause license is retained below and in the repository's
`LICENSE`. No appliance disk images are included.

```text
Copyright (c) 2016, Andrea Dainese
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the UNetLab Ltd nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
