# Ubuntu Mainline signing key

- Purpose: verify `CHECKSUMS.gpg` from `https://kernel.ubuntu.com/mainline/`.
- OpenPGP fingerprint: `60AA7B6F30434AE68E569963E50C6A0917C622B0`
- UID: `Kernel PPA <kernel-ppa@canonical.com>`
- Public key source: `https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xE50C6A0917C622B0`
- Retrieved: 2026-08-15
- ASCII public-key SHA-256: `77f90e1a84e580b23dd94df160eb1a7731533b42ae640364b153244cb91fcbab`

`ubuntu-mainline-signing-key.gpg` is a binary keyring made directly from that
ASCII public key with `gpg --dearmor`. It contains no secret material. The TUI
uses this smallest possible keyring rather than the unrelated Ubuntu archive
keyring.
