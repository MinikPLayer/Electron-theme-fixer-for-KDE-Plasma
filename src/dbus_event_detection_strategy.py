"""DBusEventDetectionStrategy - the default detection strategy.

Plasma broadcasts theme changes over D-Bus via the xdg-desktop-portal
"Settings" interface (the same interface Electron/Chromium use internally to
learn about the system color scheme). This strategy listens for that
broadcast directly - it's the detection mechanism the original fixer.py
used exclusively, before ThemeEventDetectionStrategy existed.

Needs python-dbus and python-gobject (GLib main loop). Deliberately imported
only when this strategy is actually selected - see the module docstring of
theme_event_detection_strategy.py.
"""

from __future__ import annotations

import logging
from typing import Callable

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from theme_event_detection_strategy import ThemeEventDetectionStrategy

log = logging.getLogger("electron-theme-fix")


class DBusEventDetectionStrategy(ThemeEventDetectionStrategy):
    PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
    PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
    PORTAL_SETTINGS_IFACE = "org.freedesktop.portal.Settings"

    WATCHED_NAMESPACE = "org.freedesktop.appearance"
    WATCHED_KEY = "color-scheme"

    def __init__(self) -> None:
        self._bus: dbus.SessionBus | None = None
        self._loop: GLib.MainLoop | None = None

    def resolve_dependencies(self) -> None:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SessionBus()

    def start(self, on_theme_changed: Callable[[object], None]) -> None:
        def _on_setting_changed(namespace: str, key: str, value) -> None:
            if namespace != self.WATCHED_NAMESPACE or key != self.WATCHED_KEY:
                return
            on_theme_changed(value)

        self._bus.add_signal_receiver(
            _on_setting_changed,
            signal_name="SettingChanged",
            dbus_interface=self.PORTAL_SETTINGS_IFACE,
            bus_name=self.PORTAL_BUS_NAME,
            path=self.PORTAL_OBJECT_PATH,
        )
        log.info(
            "Watching for Plasma theme changes via %s SettingChanged (%s/%s)...",
            self.PORTAL_SETTINGS_IFACE, self.WATCHED_NAMESPACE, self.WATCHED_KEY,
        )
        self._loop = GLib.MainLoop()

    def run_forever(self) -> None:
        self._loop.run()

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
