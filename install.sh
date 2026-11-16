#!/usr/bin/env bash
# Foldtop host installer.
#
#   ./install.sh            show what would be done — changes nothing
#   ./install.sh --apply    install the systemd user unit (does not enable or start it)
#                           and the `foldtop-host` command in ~/.local/bin
#
# Nothing here needs root. The unit runs the package straight from this directory.
set -euo pipefail

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$HOST_DIR/foldtop-host.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/foldtop-host.service"
STATE_DIR="${FOLDTOP_STATE_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/foldtop}"
BIN_DIR="$HOME/.local/bin"
BIN_DST="$BIN_DIR/foldtop-host"

APPLY=0
case "${1:-}" in
  --apply) APPLY=1 ;;
  "" ) ;;
  *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }
run() {
  if (( APPLY )); then
    say "  + $*"
    "$@"
  else
    say "  would run: $*"
  fi
}

if (( APPLY )); then
  say "Foldtop host — installing"
else
  say "Foldtop host — dry run (nothing is changed; pass --apply to install)"
fi
say
say "1. Checking this machine"
if ! (cd "$HOST_DIR" && /usr/bin/python3 -m foldtop_host doctor); then
  say
  say "   doctor reported problems; fix them first (see above)."
  if (( APPLY )); then exit 1; fi
fi
say
say "2. State directory (TLS key, paired devices): $STATE_DIR"
run mkdir -p "$STATE_DIR"
run chmod 700 "$STATE_DIR"
say
say "3. systemd user unit: $UNIT_DST"
run mkdir -p "$UNIT_DIR"
if (( APPLY )); then
  sed "s|@FOLDTOP_HOST_DIR@|$HOST_DIR|g" "$UNIT_SRC" > "$UNIT_DST"
  say "  + wrote $UNIT_DST"
  systemctl --user daemon-reload
  say "  + systemctl --user daemon-reload"
else
  say "  would write: $UNIT_DST"
  say "               (from $UNIT_SRC with WorkingDirectory=$HOST_DIR)"
  say "  would run: systemctl --user daemon-reload"
fi
say
say "4. Command: $BIN_DST"
run mkdir -p "$BIN_DIR"
if (( APPLY )); then
  cat > "$BIN_DST" <<LAUNCHER
#!/bin/sh
# Written by Foldtop's install.sh: runs the companion from where it was installed.
PYTHONPATH="$HOST_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec /usr/bin/python3 -m foldtop_host "\$@"
LAUNCHER
  chmod 755 "$BIN_DST"
  say "  + wrote $BIN_DST"
else
  say "  would write: $BIN_DST (runs the companion from $HOST_DIR)"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "  note: $BIN_DIR is not on your PATH yet; log out and in again, or run $BIN_DST directly." ;;
esac
say
say "Next steps (never done by this script):"
say "  systemctl --user enable --now foldtop-host   # start now and at every graphical login"
say "  foldtop-host pair                            # pairing code + QR for the iPhone"
say "  foldtop-host status                          # also: devices, unpair <name>, doctor"
say
say "Optional:"
say "  sudo apt install gstreamer1.0-plugins-bad         # NVENC hardware encoding (nvh264enc)"
say "  sudo ufw allow 47800/tcp                          # only if a firewall is active"
