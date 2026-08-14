"""Generic "detect, dedupe, delay, fire" orchestration.

This is the part of the original fixer.py that doesn't care *how* a theme
change was noticed (that's a ThemeEventDetectionStrategy's job - see
theme_event_detection_strategy.py) or *what* the fix actually does (that's a
ThemeFixStrategy's job - see theme_fix_strategy.py). ThemeChangeFixer just
wires the two together: whenever a detection strategy reports a change, wait
the fix strategy's apply_delay_seconds, then fire it - while deduping an echo
of our own fix apart from a genuinely new change arriving in the same short
window.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from theme_fix_strategy import ThemeFixStrategy

log = logging.getLogger("electron-theme-fix")


class Consts:
    """Static configuration - how long to remember a fired fix's fingerprint."""

    # How long a fired fix's fingerprint is remembered for, to tell an echo
    # of our own fix apart from a genuinely new theme switch arriving in the
    # same window. Shared across fix strategies - it's a harmless no-op for
    # one whose action doesn't cause an echo on the watched signal in the
    # first place (e.g. DBusDirectSignalFixer under GtkFileWatcherDetectionStrategy,
    # which never touches gtk-3.0/gtk-4.0 settings.ini).
    SUPPRESS_WINDOW_SECONDS = 2.0


@dataclass
class SeenChange:
    """A recent theme change we've already scheduled a fix for.

    `value` is whatever equality-comparable token the active
    ThemeEventDetectionStrategy passed to on_theme_changed() - e.g. the raw
    color-scheme value for the D-Bus/GSettings strategies, since that comes
    straight from the change notification's own payload and is authoritative
    the instant it's received. (A strategy with no such natural token, like
    the file watcher, can pass a constant instead - see
    GtkFileWatcherDetectionStrategy.)
    """
    timestamp: float
    value: object


class ThemeChangeFixer:
    """Watches for reported theme changes and, after a short delay, asks a
    ThemeFixStrategy to nudge Electron apps into refreshing.

    Instantiate once per run with the chosen ThemeFixStrategy, then pass its
    on_theme_changed method as the callback to a ThemeEventDetectionStrategy's
    start().
    """

    def __init__(self, strategy: ThemeFixStrategy) -> None:
        self._strategy = strategy
        # Recent values we've already scheduled a fix for, so a repeat of
        # the *same* value shortly afterwards - most commonly an echo the
        # active fix strategy's own action causes, but possibly just the
        # underlying mechanism firing twice for one real change - can be
        # told apart from a genuinely new switch (e.g. back to the previous
        # scheme) arriving in the same window, which must still go through.
        self._recent_changes: list[SeenChange] = []
        # Detection strategies are free to report changes from a thread that
        # isn't the main thread (e.g. GtkFileWatcherDetectionStrategy's
        # watchdog Observer thread, or a threading.Timer callback below), so
        # all access to _recent_changes needs to be serialized.
        self._lock = threading.Lock()

    def _find_recent_change_locked(self, value: object) -> SeenChange | None:
        for entry in self._recent_changes:
            if entry.value == value:
                return entry
        return None

    def _fire_once(self, value: object) -> None:
        self._strategy.apply_fix()
        # If (and only if) this strategy's fix causes an echo of the watched
        # change, that echo won't reach on_theme_changed() until roughly now
        # + (D-Bus/process overhead), not until apply_delay_seconds from when
        # we first *detected* the change - so the window has to count down
        # from here (fire time), not from detection time, or it can elapse
        # before the echo we're trying to catch even arrives.
        with self._lock:
            entry = self._find_recent_change_locked(value)
            if entry is not None:
                entry.timestamp = time.monotonic()

    def on_theme_changed(self, value: object) -> None:
        """Callback to hand a ThemeEventDetectionStrategy's start(). Call
        once per detected change, with an equality-comparable token
        describing the new state."""
        with self._lock:
            now = time.monotonic()
            existing = self._find_recent_change_locked(value)
            if existing is not None:
                age = now - existing.timestamp
                if age < Consts.SUPPRESS_WINDOW_SECONDS:
                    log.debug(
                        "Ignoring theme change (value=%r) - seen %.2fs ago, within the "
                        "%.1fs window; likely an echo of our own fix",
                        value, age, Consts.SUPPRESS_WINDOW_SECONDS,
                    )
                    return
                # Same value as before, but that entry has aged out - this
                # is a genuinely new switch back to it, not a leftover echo.
                self._recent_changes.remove(existing)

            self._recent_changes.append(SeenChange(timestamp=now, value=value))

        log.info(
            "Detected system theme change (value=%r); scheduling fix in %.1fs",
            value, self._strategy.apply_delay_seconds,
        )
        # threading.Timer rather than e.g. GLib.timeout_add: detection
        # strategies don't all run a GLib main loop (GtkFileWatcherDetectionStrategy
        # doesn't run one at all), so scheduling here can't depend on one
        # being present. subprocess calls made from a Timer's thread are
        # fine - ThemeFixStrategy.apply_fix() doesn't touch any state shared
        # with the detection strategy other than through this class.
        timer = threading.Timer(self._strategy.apply_delay_seconds, self._fire_once, args=(value,))
        timer.daemon = True
        timer.start()
