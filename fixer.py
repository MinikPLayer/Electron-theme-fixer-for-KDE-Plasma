#!/usr/bin/env python3
"""Electron Theme Fixer for KDE Plasma

Works around a KDE Plasma bug where Electron apps end up "one theme change
behind" the rest of the system when switching between light and dark mode.
Workaround for https://github.com/electron/electron/issues/48736.

How it works
------------
Plasma broadcasts theme changes over D-Bus via the xdg-desktop-portal
"Settings" interface (the same interface Electron/Chromium use internally to
learn about the system color scheme). We listen for that broadcast, wait a
short moment for Plasma to finish applying the new scheme, and then
re-apply the *currently active* accent color with ``plasma-apply-colorscheme
-a``. Re-issuing the "same" color scheme by name is rejected by
plasma-apply-colorscheme (it refuses to set a scheme that's already active),
but re-issuing the current accent color is not - and doing so forces the same
fresh theme-changed notification that nudges Electron apps into repainting
with the correct theme.

Requirements
------------
- python-dbus and python-gobject (GLib main loop)
- plasma-apply-colorscheme (part of plasma-workspace)
- kreadconfig6 or kreadconfig5 (part of kconfig / plasma-workspace)

Usage
-----
    ./fixer.py

Run it in the background, e.g. as a systemd --user service - see
systemd/electron-theme-fix.service (or just run install.py).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

# NOTE: dbus/gi are intentionally NOT imported here at module scope. They're
# only needed to actually run the watcher, not to answer --version - so
# `./fixer.py --version` keeps working even on a machine that doesn't have
# python-dbus/python-gobject installed yet (e.g. install.py checking the
# version of a script it's about to install). See _import_dbus_deps().
GLib = None  # populated by _import_dbus_deps(), referenced by ThemeChangeFixer below

VERSION = "1.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("electron-theme-fix")


class Consts:
    """Static configuration - D-Bus addresses and timings."""

    # This should not be hard coded, but there's no D-Bus API to query it, sorry!
    APPLY_DELAY_SECONDS = 1.0

    # plasma-apply-colorscheme re-broadcasts org.freedesktop.appearance/color-scheme
    # as a side effect of re-applying the accent color, even though the value
    # didn't actually change. Without suppressing that echo we'd re-trigger
    # ourselves and loop forever. Ignore SettingChanged signals for this long
    # after each fix we apply.
    SUPPRESS_WINDOW_SECONDS = 2.0

    PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
    PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
    PORTAL_SETTINGS_IFACE = "org.freedesktop.portal.Settings"

    WATCHED_NAMESPACE = "org.freedesktop.appearance"
    WATCHED_KEY = "color-scheme"


def find_executable(*candidates: str) -> str:
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    print(
        f"Error: none of {', '.join(candidates)} were found in PATH.",
        file=sys.stderr,
    )
    sys.exit(1)


class Executables:
    """Resolved paths to the external tools this script shells out to.

    Populated once via resolve(), called at startup from main().
    """

    KREADCONFIG: str = ""
    PLASMA_APPLY_COLORSCHEME: str = ""

    @classmethod
    def resolve(cls) -> None:
        cls.KREADCONFIG = find_executable("kreadconfig6", "kreadconfig5")
        cls.PLASMA_APPLY_COLORSCHEME = find_executable("plasma-apply-colorscheme")


@dataclass
class SeenChange:
    """A recent color-scheme change we've already scheduled a fix for.

    Deliberately does NOT track the accent color alongside it. color_scheme
    comes straight from the SettingChanged signal's own payload, so it's
    authoritative the instant we receive it - but the accent color has to be
    read back separately from kdeglobals via kreadconfig, and there's no
    guarantee that read isn't racing a config write that hasn't landed on
    disk yet at the moment the signal arrives. A stale accent-color read
    feeding into the dedup could make it wrong in either direction (missing
    a real echo, or suppressing a real change), so only the signal's own
    color_scheme value - which carries no such risk - is compared.
    """
    timestamp: float
    color_scheme: object


class ThemeChangeFixer:
    """Watches for Plasma theme changes and re-applies the accent color to
    nudge Electron apps into refreshing.
    """

    def __init__(self) -> None:
        # Recent color-scheme values we've already scheduled a fix for, so a
        # repeat of the *same* value shortly afterwards - most commonly the
        # echo plasma-apply-colorscheme's own re-apply causes, but possibly
        # just Plasma firing the signal twice for one real change - can be
        # told apart from a genuinely new switch (e.g. back to the previous
        # scheme) arriving in the same window, which must still go through.
        self._recent_changes: list[SeenChange] = []

    def _find_recent_change(self, color_scheme: object) -> SeenChange | None:
        for entry in self._recent_changes:
            if entry.color_scheme == color_scheme:
                return entry
        return None

    def current_accent_color(self) -> Optional[str]:
        """Return the active accent color from kdeglobals as a "#rrggbb" hex string.

        kdeglobals stores it as "General/AccentColor=r,g,b" (decimal components).
        Returns None if no custom accent color is set (i.e. the scheme's default
        accent is in use) or the value can't be parsed.
        """
        try:
            result = subprocess.run(
                [
                    Executables.KREADCONFIG,
                    "--file", "kdeglobals",
                    "--group", "General",
                    "--key", "AccentColor",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("Failed to read current accent color: %s", exc.stderr.strip())
            return None

        raw = result.stdout.strip()
        if not raw:
            return None

        components = raw.split(",")
        if len(components) != 3:
            log.error("Unexpected AccentColor value %r (expected 'r,g,b')", raw)
            return None

        try:
            r, g, b = (int(component) for component in components)
        except ValueError:
            log.error("Unexpected AccentColor value %r (expected 'r,g,b')", raw)
            return None

        return f"#{r:02x}{g:02x}{b:02x}"

    def reapply_accent_color(self) -> None:
        color = self.current_accent_color()
        if not color:
            log.warning(
                "Could not determine a custom active accent color; skipping re-apply"
            )
            return

        log.info("Re-applying accent color %s to nudge Electron apps into refreshing", color)
        try:
            subprocess.run(
                [Executables.PLASMA_APPLY_COLORSCHEME, "-a", color],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("plasma-apply-colorscheme failed: %s", exc.stderr.strip())

    def _fire_once(self, color_scheme: object) -> bool:
        self.reapply_accent_color()
        # The echo this causes won't reach on_setting_changed() until roughly
        # now + (D-Bus/process overhead), not until APPLY_DELAY_SECONDS from
        # when we first *detected* the change - so the window has to count
        # down from here (fire time), not from detection time, or it can
        # elapse before the echo we're trying to catch even arrives (e.g.
        # whenever APPLY_DELAY_SECONDS is a sizeable fraction of
        # SUPPRESS_WINDOW_SECONDS - as they briefly, exactly were - the
        # window would expire right as the echo was still in flight, so every
        # echo looked "new" and re-fired forever).
        entry = self._find_recent_change(color_scheme)
        if entry is not None:
            entry.timestamp = time.monotonic()
        return False  # False -> GLib does not repeat this timeout

    def on_setting_changed(self, namespace: str, key: str, value) -> None:
        if namespace != Consts.WATCHED_NAMESPACE or key != Consts.WATCHED_KEY:
            return

        now = time.monotonic()
        existing = self._find_recent_change(value)
        if existing is not None:
            age = now - existing.timestamp
            if age < Consts.SUPPRESS_WINDOW_SECONDS:
                log.debug(
                    "Ignoring color-scheme change (value=%r) - seen %.2fs ago, within the "
                    "%.1fs window; likely an echo of our own accent-color re-apply",
                    value, age, Consts.SUPPRESS_WINDOW_SECONDS,
                )
                return
            # Same value as before, but that entry has aged out - this is a
            # genuinely new switch back to it, not a leftover echo.
            self._recent_changes.remove(existing)

        self._recent_changes.append(SeenChange(timestamp=now, color_scheme=value))

        log.info(
            "Detected system theme change (%s/%s=%r); scheduling fix in %.1fs",
            namespace, key, value, Consts.APPLY_DELAY_SECONDS,
        )
        GLib.timeout_add(int(Consts.APPLY_DELAY_SECONDS * 1000), self._fire_once, value)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Electron Theme Fixer for KDE Plasma - watches for light/dark "
        "theme changes and nudges Electron apps into picking them up.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def _import_dbus_deps() -> None:
    """Import dbus/gi lazily, only once we're actually about to run the
    watcher (see the module-level NOTE above)."""
    global dbus, GLib
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib as _GLib
    GLib = _GLib


def main() -> int:
    parse_args()  # handles --version/-h by printing and exiting; nothing to do otherwise

    Executables.resolve()
    _import_dbus_deps()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    fixer = ThemeChangeFixer()
    bus.add_signal_receiver(
        fixer.on_setting_changed,
        signal_name="SettingChanged",
        dbus_interface=Consts.PORTAL_SETTINGS_IFACE,
        bus_name=Consts.PORTAL_BUS_NAME,
        path=Consts.PORTAL_OBJECT_PATH,
    )

    log.info("Watching for Plasma theme changes via %s...", Consts.PORTAL_SETTINGS_IFACE)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Stopping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
