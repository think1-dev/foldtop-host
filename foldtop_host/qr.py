"""Terminal QR code for the pairing link.

Uses the built-in encoder in `qrgen` (no dependency). Returns None if the text does
not fit, in which case the caller falls back to showing the code and link only.
"""

from __future__ import annotations

from typing import Optional


def matrix(text: str) -> Optional[list[list[bool]]]:
    try:
        from . import qrgen

        return qrgen.encode(text.encode("utf-8"))
    except Exception:
        return None


def render(text: str, quiet: int = 2, indent: str = "  ") -> Optional[str]:
    """QR code drawn with half-block characters (two modules per text row),
    dark modules on a light background so it scans on any terminal theme."""
    m = matrix(text)
    if m is None:
        return None
    size = len(m)
    total = size + 2 * quiet

    def dark(r: int, c: int) -> bool:
        r -= quiet
        c -= quiet
        return 0 <= r < size and 0 <= c < size and m[r][c]

    # Explicit black-on-white colours: the code must be dark-on-light to scan reliably.
    on, off = "\x1b[30;47m", "\x1b[0m"
    lines = []
    for r in range(0, total, 2):
        row = []
        for c in range(total):
            top, bottom = dark(r, c), dark(r + 1, c)
            row.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        lines.append(indent + on + "".join(row) + off)
    return "\n".join(lines)
