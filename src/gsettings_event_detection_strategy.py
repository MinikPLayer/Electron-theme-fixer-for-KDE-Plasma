"""GSettingsEventDetectionStrategy - PoC'd in watcher_gio.py.

KDE's GTK config integration (kde-gtk-config / xdg-desktop-portal-kde) keeps
the org.gnome.desktop.interface GSettings schema's color-scheme key - the
same key native GNOME/GTK apps read - in sync with the Plasma color scheme.
This strategy watches that key directly via Gio.Settings instead of the
xdg-desktop-portal Settings D-Bus interface DBusEventDetectionStrategy uses;
same underlying dconf change-notification plumbing, one layer closer to the
GTK/Electron side that's actually being nudged.

Needs python-gobject (Gio + GLib). Deliberately imported only when this
strategy is actually selected - see the module docstring of
theme_event_detection_strategy.py.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from theme_event_detection_strategy import ThemeEventDetectionStrategy

log = logging.getLogger("electron-theme-fix")


class GSettingsEventDetectionStrategy(ThemeEventDetectionStrategy):
    SCHEMA = "org.gnome.desktop.interface"
    WATCHED_KEY = "color-scheme"

    def __init__(self) -> None:
        self._settings: Gio.Settings | None = None
        self._loop: GLib.MainLoop | None = None

    def resolve_dependencies(self) -> None:
        self._settings = Gio.Settings.new(self.SCHEMA)

    def start(self, on_theme_changed: Callable[[object], None]) -> None:
        def _on_changed(settings: Gio.Settings, key: str) -> None:
            on_theme_changed(settings.get_string(key))

        # The "changed::<key>" signal is the filtering mechanism - this only
        # fires for color-scheme, not every key in the schema.
        self._settings.connect(f"changed::{self.WATCHED_KEY}", _on_changed)
        log.info(
            "Watching for Plasma theme changes via GSettings %s/%s...",
            self.SCHEMA, self.WATCHED_KEY,
        )
        self._loop = GLib.MainLoop()

    def run_forever(self) -> None:
        self._loop.run()

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
