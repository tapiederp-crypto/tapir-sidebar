#!/usr/bin/env bash
# Start or toggle the AI chat sidebar.
#   ./toggle.sh                -> show/hide the sidebar
#   ./toggle.sh some message   -> show the sidebar and send "some message"
SIDEBAR="$HOME/.config/chat-sidebar/sidebar.py"

# gtk4-layer-shell must be preloaded before libwayland-client to create
# layer surfaces from Python (see gtk4-layer-shell linking.md)
export LD_PRELOAD="/usr/lib/libgtk4-layer-shell.so${LD_PRELOAD:+:$LD_PRELOAD}"

if pgrep -f "python3 $SIDEBAR" >/dev/null 2>&1; then
  if [ "$#" -gt 0 ]; then
    # second process: Gtk.Application routes the args to the running instance
    exec python3 "$SIDEBAR" "$@"
  fi
  pkill -USR1 -f "python3 $SIDEBAR"
else
  exec python3 "$SIDEBAR" "$@"
fi
