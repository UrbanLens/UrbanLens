#!/bin/bash
#
# Print daphne's inbound frame-size flags, derived from UL_WEBSOCKET_MAX_FRAME_CHARS.
#
# Separate file so the derivation can be tested without the entrypoint's chown/drop-privileges side effects.
#
# Compose cannot read a cap Django derives at import time, so the number is derived here.
#
# x4 is worst-case bytes per UTF-8 char, keeping the transport bound looser than the app-level char bound.
set -e

bytes=$(( ${UL_WEBSOCKET_MAX_FRAME_CHARS:-65536} * 4 ))
printf '%s\n' --websocket-max-message-size "$bytes" --websocket-max-frame-size "$bytes"
