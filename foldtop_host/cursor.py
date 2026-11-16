"""Tracks the pointer position and cursor image so the client can draw the pointer
itself (zero-latency cursor) — CURSOR_SHAPE / CURSOR_POS in docs/PROTOCOL.md."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from . import x11

log = logging.getLogger("foldtop.cursor")


class CursorTracker(threading.Thread):
    """Polls XQueryPointer (position) and XFixes (image) on its own X connection.

    Callbacks run on the tracker thread; hop to your event loop yourself.
    """

    def __init__(
        self,
        display_name: Optional[str],
        on_shape: Callable[[x11.CursorImage], None],
        on_position: Callable[[int, int], None],
        position_hz: float = 120.0,
        shape_hz: float = 20.0,
    ) -> None:
        super().__init__(name="foldtop-cursor", daemon=True)
        self._display_name = display_name
        self._on_shape = on_shape
        self._on_position = on_position
        self._interval = 1.0 / position_hz
        self._shape_every = max(1, round(position_hz / shape_hz))
        self._stop_event = threading.Event()
        self._resend = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def resend(self) -> None:
        """Report the current shape and position again (e.g. after a stream restart)."""
        self._resend.set()

    def run(self) -> None:
        try:
            display = x11.Display(self._display_name)
        except x11.X11Error as exc:
            log.warning("cursor tracking disabled: %s", exc)
            return
        try:
            has_xfixes = display.has_xfixes()
            if not has_xfixes:
                log.warning("XFIXES missing: the client will not get cursor images")
            last_pos: Optional[tuple[int, int]] = None
            last_serial: Optional[int] = None
            tick = 0
            while not self._stop_event.is_set():
                if self._resend.is_set():
                    self._resend.clear()
                    last_pos, last_serial = None, None
                    tick = 0
                x, y, _mask = display.query_pointer()
                if (x, y) != last_pos:
                    last_pos = (x, y)
                    self._on_position(x, y)
                if has_xfixes and tick % self._shape_every == 0:
                    serial = display.cursor_serial()
                    if serial is not None and (serial & 0xFFFFFFFF) != last_serial:
                        image = display.cursor_image()
                        if image is not None:
                            last_serial = image.serial
                            self._on_shape(image)
                tick += 1
                self._stop_event.wait(self._interval)
        except Exception:  # pragma: no cover - defensive: never take the daemon down
            log.exception("cursor tracker failed")
        finally:
            display.close()
