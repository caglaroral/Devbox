#!/usr/bin/env bash
# Start the devbox console in the foreground (development / single-user).
# For multi-user PAM login, run it as root via systemd (see devbox-console.service).
set -euo pipefail
cd "$(dirname "$0")"
# devbox-msg on PATH for the sessions of the user running this
mkdir -p "$HOME/.local/bin" && ln -sf "$PWD/devbox-msg" "$HOME/.local/bin/devbox-msg"
exec python3 -m devbox_console.app
