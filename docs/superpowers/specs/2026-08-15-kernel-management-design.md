# Kernel Management Design

## Goal

Add a distribution-aware kernel-management area to the Linux TUI without
changing the existing VPN/TCP acceleration behavior. The new area supports
Debian 11, 12, and 13 plus Ubuntu 22.04, 24.04, and 26.04 on `amd64` and
`arm64`.

The implementation must distinguish between:

- Distribution-maintained kernel packages, which are the default and safest
  path.
- Ubuntu Mainline prebuilt packages, which are optional, unsupported, and
  available only when the selected build has complete usable artifacts.
- Local source builds from kernel.org, which are an advanced path for either
  supported architecture and have materially higher disk, memory, and time
  requirements.

No action automatically reboots the machine or removes an installed kernel.
An installation becomes successful only after a later reboot has made the
intended kernel the running kernel.

## Navigation

Keep `TCP调优` focused on runtime TCP/sysctl/qdisc work. Add these two
categories immediately after it in `scripts.json`:

```text
TCP调优
内核管理
VPN专用内核
Nodeseek论坛
```

`内核管理` contains exactly these initial actions:

1. `系统内核维护`: new distribution-aware kernel manager.
2. `引导维护`: the existing GRUB manager, expanded with installed-kernel and
   next-boot/default-boot verification.

`VPN专用内核` owns the existing VPN-oriented kernel chain:

- The existing `kernel_manage` action, retained as a compatibility ID but
  renamed so it is visibly a VPN-specific legacy helper.
- `tcp_fsc_0`, `tcp_fsc_1`, `tcp_fsc_2`, `tcp_fsc_3`, `tcp_fsc_5`,
  `tcp_fsc_8`, `tcp_fsc_9`, `tcp_fsc_19`, and `tcp_fsc_20`.
- The complete upstream `tcp.sh` menu action.

The following remain in `TCP调优`: TCP Brutal, status inspection, local BBR
and BBR2 profiles, ECN, IPv6, generic sysctl profiles, tcpfit, DD entry, and
IP/media checks. They are not system-kernel installers. IDs stay stable where
possible so old launchers, tests, and user habits continue to work.

## Architecture

Create `kernel_manager.py` as a testable standard-library-only module.
`tui.py` retains curses navigation, root checks, logging, and execution
dispatch. It calls the module for all new system-kernel and boot-maintenance
operations instead of embedding another large command flow in `TUI`.

The module owns five bounded responsibilities:

| Component | Responsibility |
|---|---|
| `KernelFacts` | Read `/etc/os-release`, `dpkg --print-architecture`, `uname -r`, virtualization type, Secure Boot state, disk space, DKMS state, installed packages, `/boot` images, and bootloader capability. |
| `AptKernelPlanner` | Discover installed APT sources and only offer supported official kernel meta packages and configured candidates. Build a printed, reviewable install plan. |
| `MainlinePackagePlanner` | Query Ubuntu Mainline directory listings; list only a selected version with a successful target architecture build and all required packages/checksums. |
| `SourceBuildAdapter` | Invoke the local vendored source-builder with a constrained command line after resource checks. |
| `BootInspector` | List bootable images and GRUB entries, refresh the bootloader, set a one-time or permanent target only after it is resolvable, and compare the running kernel after reboot. |

All command execution uses argument arrays rather than shell interpolation.
All source, download, and package command results are printed and added to the
normal TUI action log.

## System Kernel Maintenance

Entering the manager first shows a read-only report:

```text
Distribution / codename / version
Architecture: amd64 or arm64
Virtual machine, container, or physical host
Running kernel and package owning it
Installed bootable kernels and free /boot space
Bootloader: GRUB / detected-but-unsupported / unavailable
Secure Boot state
DKMS modules requiring rebuild, if any
```

Containers and environments without their own bootloader are read-only for
kernel installation. The UI explains that a container shares the host kernel
and stops before modifying APT sources or packages.

The maintenance menu has these operations:

1. Refresh and inspect kernel status.
2. List the recommended distribution kernel plan.
3. Install the recommended distribution kernel plan.
4. Configure or inspect distribution kernel upstreams.
5. Inspect/install Ubuntu Mainline prebuilt packages.
6. Build/install a kernel.org source kernel.
7. Inspect installed kernels and safe rollback evidence.

### Distribution-Maintained Kernels

This is the default path. It uses only packages that the locally configured
APT sources actually advertise. The tool must not hard-code a package as
existing solely from a distribution version.

For Debian 11/12/13, it discovers the architecture-appropriate stock meta
package and can offer the matching `codename-backports` candidate only after
the Backports source is explicitly enabled and `apt-cache policy` confirms a
candidate. For Ubuntu 22.04/24.04/26.04, it discovers the standard meta
package and offers HWE only if the installed Ubuntu sources advertise a valid
matching HWE meta package.

Source configuration uses a dedicated file under
`/etc/apt/sources.list.d/yjl-tui-kernel-*.sources` or `.list`, records a
timestamped backup, displays the exact source before writing, runs
`apt-get update`, and then verifies candidate packages. It never rewrites the
user's main sources list. Disabling a TUI-created source restores the backup
or removes only the TUI-owned source file.

Before installation, print the full package list, package versions, download
size when APT provides it, `/boot` free space, and the current running kernel
that will remain installed. Install required image, modules, and headers only
when the repository exposes each package. Then run the distribution's normal
initramfs and GRUB refresh tools when present.

### Ubuntu Mainline Prebuilt Packages

This optional path uses Ubuntu Mainline's current `https://kernel.ubuntu.com/mainline/`
directory, not an APT PPA. It is shown only on `amd64`.

For each selected version, the planner checks all of the following before it
can become installable:

- The directory has a successful `amd64` build/test status.
- Headers, image/modules packages, `CHECKSUMS`, and `CHECKSUMS.gpg` are all
  present for the chosen flavor.
- The package list and SHA-256 entries agree exactly.
- The checksum signature verifies using an available trusted keyring.
- Secure Boot is disabled, or the user has explicitly chosen a separately
  supported signing workflow. Unsigned Mainline packages must not be offered
  as bootable when Secure Boot is enabled.

Ubuntu Mainline ARM64 packages are deliberately not offered by this release.
The upstream itself labels builds by actual build/test state, and active
maintainers document that ARM64 packages can lack board/platform requirements
or fail to boot. `arm64` users instead receive the official APT and source
build paths.

The local implementation is an MIT-preserving adaptation of the package
discovery idea in `cristim/kernel-update`, not a copy of its unsafe installer.
It must use HTTPS, target architecture validation, package completeness,
checksum/signature validation, pre-install planning, and no automatic cleanup.

### Kernel.org Source Builds

Vendor `tmiland/kernel-installer` as a complete local MIT subtree:

```text
scripts/kernel-installer/
  kernel_installer.sh
  src/slib.sh
  LICENSE
  UPSTREAM.md
```

`UPSTREAM.md` records the upstream URL, immutable commit, retrieval date,
license, files changed, and update procedure. The complete helper subtree is
stored locally so the script never fetches or sources code from a remote URL
at runtime.

The local fork changes the upstream behavior as follows:

- Disable self-update and remote helper fallback.
- Require verified kernel.org tarballs by default; remove the
  `--no-check-certificate` transfer path.
- Do not expose `kexec` from TUI.
- Use a TUI-controlled work directory and delete only that validated directory.
- Print and enforce conservative resource checks before dependencies or build
  work begin: free disk, free memory plus swap, CPU count, and writable `/boot`.
- Preserve the existing running and packaged kernels; do not use its generic
  uninstall flow from the initial TUI release.
- On completion, return to Boot Maintenance for initramfs/GRUB inspection.

The user selects `stable`, `longterm`, or `mainline` version. This route is
explicitly marked as a source build, not a quick package upgrade. It is the
architecture-neutral advanced option for both `amd64` and `arm64`.

## Boot Maintenance

Move the existing `grub_manage` action from `高级工具` to `内核管理`, retaining
the action ID. It expands from editing `/etc/default/grub` into a guided
boot-confirmation workflow:

1. Show `/etc/default/grub`, `grub-editenv list`, installed `/boot/vmlinuz-*`,
   and generated GRUB menu entries.
2. Regenerate GRUB and report whether each newly installed image is visible.
3. Set timeout with a timestamped `/etc/default/grub` backup.
4. Select a one-time next boot using a resolvable GRUB entry, when GRUB's
   saved-entry support is available.
5. Select a permanent default using a full resolved menu entry rather than an
   unstable numeric index.
6. Edit kernel command-line arguments with input validation and rollback on
   generation failure.
7. Restore the latest TUI GRUB backup and regenerate configuration.

The manager never reboots automatically. After a package or source install it
reports `待重启验证`. On the next TUI session, it compares `uname -r` to the
planned target and reports either `已在新内核运行` or `仍在旧内核，未确认成功`.

Systems using a non-GRUB bootloader are inspected and reported, but this first
release does not write systemd-boot, extlinux, U-Boot, or provider-specific
bootloader configuration.

## Licensing and Provenance

No unlicensed upstream file is vendored. In particular,
`pimlie/ubuntu-mainline-kernel.sh` is researched as behavior evidence but is
not copied because its repository does not grant a redistribution license.

Vendored MIT files retain their full original `LICENSE`, copyright notices,
and an `UPSTREAM.md`. Modified versions identify the local changes. The
project's normal GitHub launcher downloads these local files from this
repository, so a target server does not retrieve an unreviewed upstream script
as part of normal execution.

## Launcher and Tests

`launch.sh` downloads all new local scripts and their required helper files
into its cache, preserving relative directories and execution modes. It must
not claim the feature is offline: APT, kernel.org tarballs, and Ubuntu Mainline
artifacts still require network access when selected.

Add focused tests for:

- Category order and action migration with stable IDs.
- Debian 11/12/13 and Ubuntu 22.04/24.04/26.04 package-plan selection with
  `amd64` and `arm64` fixtures.
- Rejection of unknown distribution, containers, unsupported architecture, no
  bootloader, insufficient disk, and no complete package candidate.
- Mainline listing that rejects missing, failed, incomplete, checksum-missing,
  signature-invalid, and Secure-Boot-incompatible builds.
- No deletion target may equal the running kernel or the only remaining
  fallback kernel.
- GRUB menu parsing, full-entry selection, backup/rollback, and post-reboot
  confirmation state.
- Vendored source-builder files, MIT license and provenance documents, and
  launch cache layout.

Final non-destructive verification includes `python3 -m unittest discover -s
tests -v`, `./run.sh --check`, `./run.sh --list`, shell syntax checks for all
vendored scripts, and pseudo-terminal navigation at normal and small terminal
dimensions. Static checks do not prove a real kernel change. A separate root
test machine is required to validate APT installation, GRUB recognition,
reboot, and post-reboot `uname -r` for each supported distribution family and
architecture.

## Explicit Non-Goals

- Do not replace or retune the existing VPN/TCP acceleration features.
- Do not promise Ubuntu Mainline `arm64` boot support.
- Do not run a full system upgrade as a side effect of kernel installation.
- Do not auto-reboot, auto-purge kernels, or change an unrecognized bootloader.
- Do not treat package installation, GRUB generation, or a downloaded archive
  as proof that the new kernel has booted.
