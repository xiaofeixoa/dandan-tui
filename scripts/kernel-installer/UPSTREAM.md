# Upstream provenance

- Upstream: https://github.com/tmiland/kernel-installer
- Pinned commit: `dbab0c64efe585896d81083eb2a2768621c3c7b5`
- Upstream commit date: 2025-08-15
- Imported on: 2026-08-15
- License: MIT (`LICENSE` is included verbatim)

## Imported file digests

These SHA-256 values are for the files as fetched from the pinned upstream
commit, before the local safety changes below.

```text
kernel_installer.sh  258523e77378320b2efbb54f9cd8354e6d036a47b86baaecac9cd9f5d9ecfb6e
src/slib.sh          b59fe3a9ede69a7e58af2c395b7cc7d655f96346a365abdd21f8471cce0ed036
LICENSE              b96b72c0fdf6755692adad6e047ab59845cb0ea478b58ea003df64a42c1902f0
```

## Local safety changes

1. Resolve the script directory and require the checked-in `src/slib.sh`; do
   not fetch or source a helper from the network.
2. Force verified kernel.org source tarballs. The regular source download now
   requires HTTPS with TLS 1.2 or newer and no longer disables certificate
   validation.
3. Remove command-line paths for script update, kernel uninstall, and kexec.
   The vendored copy is updated only through normal repository review.
4. The TUI invokes only stable or longterm source-build paths because the
   bundled verification flow covers their kernel.org release tarballs. It does
   not expose raw configuration targets, mainline snapshots, kexec, or
   uninstall operations.

The build still downloads kernel source and build dependencies from the
network. It never reboots automatically. Activation is confirmed only after a
separate reboot and `uname -r` check.
