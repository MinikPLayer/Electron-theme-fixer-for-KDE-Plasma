"""ThemeEventDetectionStrategy interface.

A ThemeEventDetectionStrategy is the "notice a theme change happened in the
first place" half of the fixer (see theme_fix_strategy.py for the other half
- "nudge Electron apps into refreshing"). Exactly one strategy is active per
run, chosen in electron-theme-fixer.py's main() via --detection-strategy.

Implementations:

- DBusEventDetectionStrategy (dbus_event_detection_strategy.py, default) -
  listens for the xdg-desktop-portal "Settings" SettingChanged D-Bus signal.
  Needs python-dbus and python-gobject (GLib main loop).
- GSettingsEventDetectionStrategy (gsettings_event_detection_strategy.py) -
  watches the org.gnome.desktop.interface color-scheme GSettings key via
  Gio.Settings. Needs python-gobject (Gio + GLib).
- GtkFileWatcherDetectionStrategy (gtk_file_watcher_detection_strategy.py) -
  watches ~/.config/gtk-3.0 and ~/.config/gtk-4.0 for writes to settings.ini
  with inotify. Needs the watchdog package.

Each implementation lives in its own file and is only ever imported by
electron-theme-fixer.py's main(), inside the branch that actually selected
it (see the import there) - never at this module's top level, and never by
one another. That's deliberate: it's what lets a strategy depend on a
library that isn't installed on this system (e.g. `watchdog` for
GtkFileWatcherDetectionStrategy) without breaking `--version`, `--help`, or
either of the *other* two strategies for someone who doesn't have it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class ThemeEventDetectionStrategy(ABC):
    """Common interface for a "notice the system theme changed" method.

    A strategy owns whatever watch mechanism its detection method needs (a
    GLib main loop for the D-Bus- and GSettings-based strategies, a watchdog
    Observer thread for the file-watcher-based one) - run_forever() is where
    that mechanism actually blocks.
    """

    @abstractmethod
    def resolve_dependencies(self) -> None:
        """Locate/validate any external tools or connections this strategy
        needs (e.g. connecting to the session bus). Called once at startup,
        only for whichever strategy was actually selected."""

    @abstractmethod
    def start(self, on_theme_changed: Callable[[object], None]) -> None:
        """Start watching for theme changes. Must arrange for
        on_theme_changed(value) to be called once per detected change, where
        value is an equality-comparable token describing the new state (used
        by ThemeChangeFixer to tell an echo of our own fix apart from a
        genuinely new change - see theme_change_fixer.py). Must not block -
        the actual watching happens once run_forever() is called."""

    @abstractmethod
    def run_forever(self) -> None:
        """Block, running whatever event loop or watch this strategy needs,
        until interrupted (e.g. Ctrl+C)."""

    def stop(self) -> None:
        """Cleanly release any resources (background threads, etc.) this
        strategy's watch needed. Called once, after run_forever() returns or
        raises. Default no-op - override only if teardown is actually
        needed."""
