#!/usr/bin/env bash
# Runs the whole host test-suite.
#
#   ./run_tests.sh                         everything that is safe on a live desktop
#   FOLDTOP_TEST_XEPHYR=1 ./run_tests.sh   also the typing/clicking/scrolling tests, inside a
#                                          nested X server (opens a small Xephyr window)
#   FOLDTOP_TEST_LOG=1 ./run_tests.sh      with the daemon's debug log
#
# The live-desktop tests only read the screen, move the pointer (and put it back) and
# tap Left Shift. They use a random port and a temporary state directory.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec /usr/bin/python3 -m unittest discover -s tests -t . "$@"
