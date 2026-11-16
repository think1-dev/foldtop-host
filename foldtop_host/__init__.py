"""Foldtop workstation companion.

Streams an X11 monitor to the Foldtop iOS app and injects the pointer/keyboard
input the app sends back. The wire protocol is specified in docs/PROTOCOL.md.
"""

__version__ = "1.0.0"
PROTOCOL_VERSION = 1
DEFAULT_PORT = 47800
SERVICE_TYPE = "_foldtop._tcp"
