#!/usr/bin/env bash
#
# Make Playwright's chromium runnable on a host with no root.
#
# Chromium needs a handful of GTK/ATK/ALSA shared libraries that a headless
# server image does not ship, and installing them is `apt-get install`, which
# needs a password this environment does not have. But *downloading* a package
# does not: `apt-get download` writes to the current directory as an ordinary
# user, and `dpkg-deb -x` unpacks it anywhere. Point LD_LIBRARY_PATH at the
# result and the browser starts.
#
# Without this, `bun run test:browser` reports every gesture test as failing to
# launch, and the floorplan editor's drag behaviour goes unverified - which is
# exactly the part of it that cannot be covered any other way.
#
# Usage:
#   bin/browser_libs.sh                      # fetch and unpack, once per machine
#   eval "$(bin/browser_libs.sh --env)"      # print the export for this shell
#
# The test file sets LD_LIBRARY_PATH itself from the same location, so running
# it needs nothing beyond having run this script once.

set -euo pipefail

PREFIX="${BROWSER_LIBS_DIR:-$HOME/browserlibs}"
LIBDIR="$PREFIX/root/usr/lib/x86_64-linux-gnu"

if [[ "${1:-}" == "--env" ]]; then
    echo "export LD_LIBRARY_PATH=\"$LIBDIR\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}\""
    exit 0
fi

# Named for Ubuntu 24.04 (noble), where several of these carry the t64 suffix
# from the 64-bit time_t transition. On an older release, drop the suffix.
PACKAGES=(
    libatk1.0-0t64
    libatk-bridge2.0-0t64
    libatspi2.0-0t64
    libasound2t64
    libxdamage1
    libcairo2
    libpango-1.0-0
)

mkdir -p "$PREFIX/debs" "$PREFIX/root"
cd "$PREFIX/debs"

echo "==> downloading ${#PACKAGES[@]} packages (no root required)"
apt-get download "${PACKAGES[@]}"

echo "==> unpacking into $PREFIX/root"
for deb in *.deb; do
    dpkg-deb -x "$deb" "$PREFIX/root/"
done

echo "==> done. $(ls "$LIBDIR" | wc -l) files in $LIBDIR"
echo "    Verify with:  LD_LIBRARY_PATH=$LIBDIR ldd \$(find ~/.cache/ms-playwright -name headless_shell | head -1) | grep -c 'not found'"
