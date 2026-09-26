#!/usr/bin/env bash
# Install the bundled DKMS source package without fetching tcp-brutal upstream.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec bash "${SCRIPT_DIR}/scripts/install_dkms.sh" install --local "${SCRIPT_DIR}/dkms.tar.gz"
