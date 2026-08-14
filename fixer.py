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
short moment, then nudge Electron apps into re-checking the theme using one
of two interchangeable strategies (see ThemeFixStrategy):

- DBusDirectSignalFixer (default) - emits a synthetic
  ca.desrt.dconf.Writer.Notify signal for an empty GNOME interface-theme
  key. Nothing about the real theme is ever read or written; the mere
  change *notification* is enough to make Electron/GTK apps re-check the
  system theme.
- PlasmaColorSchemeFixer (--plasma-fixer/-pf) - re-applies the *currently
  active* accent color with ``plasma-apply-colorscheme -a``. Re-issuing the
  color scheme by *name* doesn't work - plasma-apply-colorscheme refuses to
  set a scheme that's already active - but re-issuing the current accent
  color is accepted, and makes Plasma re-broadcast the color-scheme portal
  setting as a side effect, which is what actually does the nudging.

Requirements
------------
- python-dbus and python-gobject (GLib main loop)
- gdbus (part of glib2) for the default DBusDirectSignalFixer
- plasma-apply-colorscheme and kreadconfig6/kreadconfig5 (part of
  plasma-workspace / kconfig) only if using --plasma-fixer

Usage
-----
    ./fixer.py                 # default: dconf change-notification method
    ./fixer.py --plasma-fixer  # KDE-Plasma-specific accent-color method

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
from abc import ABC, abstractmethod
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
    """Static configuration - what to watch, and how long to remember it."""

    # How long a fired fix's fingerprint is remembered for, to tell an echo
    # of our own fix apart from a genuinely new theme switch arriving in the
    # same window. Shared across fix strategies - it's a harmless no-op for
    # one whose action doesn't cause an echo on the watched signal in the
    # first place (DBusDirectSignalFixer; see ThemeChangeFixer).
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


class ThemeFixStrategy(ABC):
    """Common interface for a "nudge Electron apps into refreshing" method.
    Exactly one strategy is active per run, chosen in main() via
    --plasma-fixer.
    """

    @property
    @abstractmethod
    def apply_delay_seconds(self) -> float:
        """Seconds to wait after detecting a theme change before firing.
        Tuned per strategy by hand - there's no D-Bus API to query the
        "right" value, sorry."""

    @abstractmethod
    def resolve_executables(self) -> None:
        """Locate/validate any external tools this strategy needs. Called
        once at startup, only for whichever strategy was actually selected."""

    @abstractmethod
    def apply_fix(self) -> None:
        """Perform the actual nudge that gets Electron apps to refresh."""


class PlasmaColorSchemeFixer(ThemeFixStrategy):
    """Re-applies the current accent color via ``plasma-apply-colorscheme
    -a`` (see the module docstring for why that's what actually nudges
    Electron apps). KDE-Plasma-specific: needs kreadconfig and
    plasma-apply-colorscheme.
    """

    apply_delay_seconds = 1.0

    def __init__(self) -> None:
        self.kreadconfig = ""
        self.plasma_apply_colorscheme = ""

    def resolve_executables(self) -> None:
        self.kreadconfig = find_executable("kreadconfig6", "kreadconfig5")
        self.plasma_apply_colorscheme = find_executable("plasma-apply-colorscheme")

    def current_accent_color(self) -> Optional[str]:
        """Return the active accent color from kdeglobals as a "#rrggbb" hex string.

        kdeglobals stores it as "General/AccentColor=r,g,b" (decimal components).
        Returns None if no custom accent color is set (i.e. the scheme's default
        accent is in use) or the value can't be parsed.
        """
        try:
            result = subprocess.run(
                [
                    self.kreadconfig,
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

    def apply_fix(self) -> None:
        color = self.current_accent_color()
        if not color:
            log.warning(
                "Could not determine a custom active accent color; skipping re-apply"
            )
            return

        log.info("Re-applying accent color %s to nudge Electron apps into refreshing", color)
        try:
            subprocess.run(
                [self.plasma_apply_colorscheme, "-a", color],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("plasma-apply-colorscheme failed: %s", exc.stderr.strip())


class DBusDirectSignalFixer(ThemeFixStrategy):
    """Emits a synthetic ca.desrt.dconf.Writer.Notify signal for an empty
    GNOME interface-theme key. Electron/GTK apps that watch dconf for theme
    changes re-check the system theme whenever they see *any* change notice
    on that key, regardless of its value - so an empty, meaningless value
    works just as well as a real one, and we never have to read or write
    anything real: only a change notification goes out, not an actual
    setting. Default strategy; needs gdbus (part of glib2).
    """

    apply_delay_seconds = 0.2

    NOTIFY_OBJECT_PATH = "/ca/desrt/dconf/Writer/user"
    NOTIFY_SIGNAL = "ca.desrt.dconf.Writer.Notify"
    NOTIFY_KEY_PATH = "/org/gnome/desktop/interface/"

    def __init__(self) -> None:
        self.gdbus = ""

    def resolve_executables(self) -> None:
        self.gdbus = find_executable("gdbus")

    def apply_fix(self) -> None:
        log.info("Emitting a synthetic theme-change notification to nudge Electron apps into refreshing")
        try:
            subprocess.run(
                [
                    self.gdbus, "emit", "--session",
                    "--object-path", self.NOTIFY_OBJECT_PATH,
                    "--signal", self.NOTIFY_SIGNAL,
                    self.NOTIFY_KEY_PATH, "['color-scheme']", "prefer-dark",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("gdbus emit failed: %s", exc.stderr.strip())


class ThemeChangeFixer:
    """Watches for Plasma theme changes and, after a short delay, asks a
    ThemeFixStrategy to nudge Electron apps into refreshing.
    """

    def __init__(self, strategy: ThemeFixStrategy) -> None:
        self._strategy = strategy
        # Recent color-scheme values we've already scheduled a fix for, so a
        # repeat of the *same* value shortly afterwards - most commonly an
        # echo the active strategy's own fix causes, but possibly just
        # Plasma firing the signal twice for one real change - can be told
        # apart from a genuinely new switch (e.g. back to the previous
        # scheme) arriving in the same window, which must still go through.
        self._recent_changes: list[SeenChange] = []

    def _find_recent_change(self, color_scheme: object) -> SeenChange | None:
        for entry in self._recent_changes:
            if entry.color_scheme == color_scheme:
                return entry
        return None

    def _fire_once(self, color_scheme: object) -> bool:
        self._strategy.apply_fix()
        # If (and only if) this strategy's fix causes an echo of the watched
        # signal, that echo won't reach on_setting_changed() until roughly
        # now + (D-Bus/process overhead), not until apply_delay_seconds from
        # when we first *detected* the change - so the window has to count
        # down from here (fire time), not from detection time, or it can
        # elapse before the echo we're trying to catch even arrives (e.g.
        # whenever apply_delay_seconds is a sizeable fraction of
        # SUPPRESS_WINDOW_SECONDS, the window could expire right as the echo
        # was still in flight, making every echo look "new" and re-fire
        # forever).
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
                    "%.1fs window; likely an echo of our own fix",
                    value, age, Consts.SUPPRESS_WINDOW_SECONDS,
                )
                return
            # Same value as before, but that entry has aged out - this is a
            # genuinely new switch back to it, not a leftover echo.
            self._recent_changes.remove(existing)

        self._recent_changes.append(SeenChange(timestamp=now, color_scheme=value))

        log.info(
            "Detected system theme change (%s/%s=%r); scheduling fix in %.1fs",
            namespace, key, value, self._strategy.apply_delay_seconds,
        )
        GLib.timeout_add(int(self._strategy.apply_delay_seconds * 1000), self._fire_once, value)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Electron Theme Fixer for KDE Plasma - watches for light/dark "
        "theme changes and nudges Electron apps into picking them up.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "-pf", "--plasma-fixer", action="store_true",
        help="Use the KDE-Plasma-specific accent-color re-apply method "
        "(plasma-apply-colorscheme) instead of the default dconf "
        "change-notification method.",
    )
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
    args = parse_args()  # also handles --version/-h by printing and exiting

    strategy: ThemeFixStrategy = PlasmaColorSchemeFixer() if args.plasma_fixer else DBusDirectSignalFixer()
    strategy.resolve_executables()
    _import_dbus_deps()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    fixer = ThemeChangeFixer(strategy)
    bus.add_signal_receiver(
        fixer.on_setting_changed,
        signal_name="SettingChanged",
        dbus_interface=Consts.PORTAL_SETTINGS_IFACE,
        bus_name=Consts.PORTAL_BUS_NAME,
        path=Consts.PORTAL_OBJECT_PATH,
    )

    log.info(
        "Watching for Plasma theme changes via %s (fix method: %s)...",
        Consts.PORTAL_SETTINGS_IFACE, type(strategy).__name__,
    )
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Stopping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
