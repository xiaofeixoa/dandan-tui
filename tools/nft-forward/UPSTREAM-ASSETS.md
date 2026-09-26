# nft-forward v0.68.0 asset record

The binaries and checksum file in this directory were downloaded from the
`xjetry/nft-forward` GitHub Release `v0.68.0`, published 2026-07-13.

| Release asset | Published SHA-256 |
| --- | --- |
| `install.sh` | `0ef9d8ddb776681871c4cc792844fd851c5f96afd7c74e76aa9d6b6b26ff9d77` |
| `nft-agent` | `c7b0844a436a33e65ebfac7e18e29f0a6914e11d36737be1af49786a748aacec` |
| `nft-server` | `669958f3fe02ef4c5e29deb109e3f8a5b57cb1fedefdfb895e0e416d01c73854` |
| `SHA256SUMS` | `aff9af7c899cef812615815222df18bf6379782ed3ac8a567e813e2f4d21eb34` |

`nft-agent`, `nft-server`, and `SHA256SUMS` are byte-for-byte release assets.
`install.sh` started from the listed release asset, then received a narrow local
patch so it uses this directory as the default `file://` release source and
updates its installer copy from `dandan8511/dandan-tui` rather than the upstream
script URL.
