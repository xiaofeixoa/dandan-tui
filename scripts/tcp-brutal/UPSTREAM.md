# tcp-brutal offline snapshot

This directory is a complete source snapshot of
[`HyNetworks/tcp-brutal`](https://github.com/HyNetworks/tcp-brutal), plus the
generated `dkms.tar.gz` used by the project-owned offline launcher.

```text
upstream commit: 377d2a0e9324ef585ff90ea91779baf276cf6a50
upstream date:   2026-09-04
upstream subject: fix(install): install C library headers before building brutalctl
license:         GPL-3.0-or-later (source snapshot; see LICENSE)
dkms.tar.gz sha256: fbf0fd979102c7aff5d7b91c2d9e12fb1a00aa8521c83a825f4b66fb21daf236
```

The only project change inside the copied upstream files is removal of one
trailing whitespace-only line from `README.md`. `install-local.sh` invokes the
upstream `scripts/install_dkms.sh` with
`--local dkms.tar.gz`, so the module source does not need `tcp.hy2.sh`, the
Hysteria API, GitHub, or a tcp-brutal release download. The supported system
package manager must still provide DKMS, a compiler, and headers for the
currently running kernel.
