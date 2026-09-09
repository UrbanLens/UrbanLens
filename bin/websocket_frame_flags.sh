#!/bin/bash
#
# Print daphne's inbound frame-size flags, derived from UL_WEBSOCKET_MAX_FRAME_CHARS.
#
# Its own file rather than a few lines inside docker-entrypoint.sh so the
# derivation can be run on its own: the entrypoint chowns volumes and drops
# privileges before it execs anything, so a test of the whole script measures
# the environment more than the arithmetic.
#
# The number cannot live in docker-compose.yml. Compose substitutes ${...} from
# the shell when it parses the file, before any container exists, so it cannot
# read a cap Django derives at import time - and writing the number in both
# places is how the transport cap drifts below the application one. Autobahn
# refuses an oversized frame with nothing sent and nothing logged, so that
# drift reaches a user as an unexplained disconnect rather than an error.
#
# x4 is the worst case bytes per UTF-8 character, which is what makes the
# transport bound strictly looser than the character bound the consumers apply.
# settings/base.py derives UL_WEBSOCKET_MAX_MESSAGE_BYTES the same way, and
# dashboard.checks.websocket_frame_cap_conflict reports it when they disagree.
set -e

bytes=$(( ${UL_WEBSOCKET_MAX_FRAME_CHARS:-65536} * 4 ))
printf '%s\n' --websocket-max-message-size "$bytes" --websocket-max-frame-size "$bytes"
