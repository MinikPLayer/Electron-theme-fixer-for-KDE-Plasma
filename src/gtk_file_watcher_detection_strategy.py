"""GtkFileWatcherDetectionStrategy - PoC'd in watcher_test.py.

KDE's GTK config integration rewrites ~/.config/gtk-3.0/settings.ini and
~/.config/gtk-4.0/settings.ini whenever the Plasma theme changes, to keep
native GTK apps' theme in sync. This strategy watches those two files
directly with inotify (via watchdog) instead of listening for a D-Bus/dconf
change notification - a fallback detection mechanism for setups where
neither of those is reliably available.

Needs the watchdog package. Deliberately imported only when this strategy is
actually selected - see the module docstring of
theme_event_detection_strategy.py.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from theme_event_detection_strategy import ThemeEventDetectionStrategy

log = logging.getLogger("electron-theme-fix")


# Events that mean "a settings.ini has an open file handle, or is about to" -
# i.e. a write may be in progress. "created"/"modified" are included too, not
# just "opened", because in practice they can arrive before the "opened"
# event for the very same write (observed with shell redirection); any of
# them is enough to know a handle exists.
_HANDLE_OPENING_EVENTS = frozenset({"created", "opened", "modified"})

# Events that mean a previously-open handle on a settings.ini has been
# released.
_HANDLE_CLOSING_EVENTS = frozenset({"closed", "closed_no_write"})

# Events that are inherently self-contained - no open handle is ever
# observed for them, so they never touch the pending set, just the
# settings_modified flag: a delete needs no open fd, and the write side of a
# write-to-temp-then-rename lands on a *different* filename, so the only
# trace on the real name is the rename itself.
_SELF_CONTAINED_WRITE_EVENTS = frozenset({"deleted", "moved"})


class DebouncedHandler(FileSystemEventHandler):
    """Calls `on_settled` once things have settled, detected two ways:

    - Deterministically: once every settings.ini that started being written
      to this burst has also produced a completion event (a write-close, or
      an atomic rename landing on it) - no guessing involved, fires
      immediately rather than waiting out the debounce window.
    - As a fallback: if nothing definitive comes in for `debounce_seconds` -
      covers unrelated file churn, and the (unlikely) case of a settings.ini
      write whose completion event we fail to observe.

    Whichever happens first fires `on_settled` and resets state for the next
    burst.
    """

    def __init__(self, on_settled: Callable[[bool], None], expected_files: set[Path],
                 debounce_seconds: float):
        self._on_settled = on_settled
        self._expected_files = expected_files
        self._debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        # Whether any create/delete/modify seen since the last fire touched a
        # settings.ini file - reported to on_settled, then cleared.
        self._settings_modified = False
        # Expected settings.ini files that started writing this burst but
        # haven't yet produced a completion event.
        self._pending_settings_files: set[Path] = set()

    def on_any_event(self, event):
        settings_path = self._settings_ini_path(event)
        if settings_path is not None:
            # Note: deliberately not elif's below - e.g. "modified" is both
            # a sign of an actual change *and* a handle-opening event, and
            # both need to be recorded.
            if event.event_type in _HANDLE_OPENING_EVENTS:
                self._pending_settings_files.add(settings_path)
            if event.event_type in _HANDLE_CLOSING_EVENTS:
                self._pending_settings_files.discard(settings_path)
            if event.event_type in _SELF_CONTAINED_WRITE_EVENTS:
                self._settings_modified = True
            # "closed" (IN_CLOSE_WRITE - as opposed to "closed_no_write",
            # i.e. a plain read) confirms an actual write happened, not just
            # an open+close with nothing written.
            if event.event_type == "closed":
                self._settings_modified = True

        with self._lock:
            if self._settings_modified and not self._pending_settings_files:
                # Every settings.ini touched this burst has a confirmed
                # completion event - fire now instead of waiting out the
                # fallback timer.
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                self._fire()
            else:
                self._reset_timer_locked()

    def _settings_ini_path(self, event) -> Path | None:
        if event.is_directory:
            return None
        for path_str in (event.src_path, getattr(event, "dest_path", None)):
            if path_str and Path(path_str) in self._expected_files:
                return Path(path_str)
        return None

    def _reset_timer_locked(self):
        if self._timer is not None:
            self._timer.cancel()
        # Threading.Timer.cancel() can't interrupt a callback that's already
        # started running (just narrowly missed the cancellation) - passing
        # the timer to its own callback lets that callback recognize it's
        # been superseded and no-op, instead of firing a second time.
        timer = threading.Timer(self._debounce_seconds, lambda: self._on_timeout(timer))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timeout(self, timer):
        with self._lock:
            if self._timer is not timer:
                return  # cancelled, or superseded by a newer timer - stale
            self._timer = None
            self._fire()

    def _fire(self):
        settings_modified = self._settings_modified
        self._settings_modified = False
        self._pending_settings_files.clear()
        self._on_settled(settings_modified)

    def stop(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class GtkFileWatcherDetectionStrategy(ThemeEventDetectionStrategy):
    # Upper bound to wait for a change we can't positively confirm is
    # finished - e.g. unrelated file churn in the watched directories, or
    # (belt-and-braces) a settings.ini write whose completion event never
    # arrives for some reason. Reset on every new event. Only actually
    # elapses if nothing conclusively signals "done" first - see
    # DebouncedHandler.on_any_event.
    DEBOUNCE_SECONDS = 0.1

    WATCHED_DIRS = [
        Path("~/.config/gtk-3.0").expanduser(),
        Path("~/.config/gtk-4.0").expanduser(),
    ]

    # The specific files we can detect the *actual* completion of - a
    # write-close (IN_CLOSE_WRITE) or an atomic rename landing on them -
    # rather than just guessing based on a quiet period. See DebouncedHandler.
    EXPECTED_SETTINGS_FILES = {directory / "settings.ini" for directory in WATCHED_DIRS}

    def __init__(self) -> None:
        self._observer = Observer()
        self._handler: DebouncedHandler | None = None

    def resolve_dependencies(self) -> None:
        pass  # nothing to resolve upfront - watched directories are checked in start()

    def start(self, on_theme_changed: Callable[[object], None]) -> None:
        def _on_settled(settings_modified: bool) -> None:
            if settings_modified:
                # No natural "what changed to" value exists for this
                # detection mechanism (unlike the D-Bus/GSettings
                # strategies, which get one from the change notification's
                # own payload) - a constant token is enough for
                # ThemeChangeFixer's dedup, since consecutive genuine
                # settings.ini rewrites within the suppress window are rare
                # and harmless to collapse together.
                on_theme_changed(True)

        self._handler = DebouncedHandler(_on_settled, self.EXPECTED_SETTINGS_FILES, self.DEBOUNCE_SECONDS)

        scheduled = 0
        for directory in self.WATCHED_DIRS:
            if not directory.exists():
                log.warning("Skipping %s - does not exist", directory)
                continue
            self._observer.schedule(self._handler, str(directory), recursive=False)
            log.info("Watching %s for GTK settings.ini changes...", directory)
            scheduled += 1

        if not scheduled:
            raise RuntimeError(
                f"None of the watched GTK config directories exist "
                f"({', '.join(str(d) for d in self.WATCHED_DIRS)}) - nothing to watch"
            )

        self._observer.start()

    def run_forever(self) -> None:
        self._observer.join()

    def stop(self) -> None:
        if self._handler is not None:
            self._handler.stop()
        self._observer.stop()
        self._observer.join()
