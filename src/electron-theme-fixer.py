#!/usr/bin/env python3
"""Electron Theme Fixer for KDE Plasma

Works around a KDE Plasma bug where Electron apps end up "one theme change
behind" the rest of the system when switching between light and dark mode.
Workaround for https://github.com/electron/electron/issues/48736.

How it works
------------
The fix has two independent halves, each with a choice of interchangeable
strategies:

- Detection (ThemeEventDetectionStrategy, see theme_event_detection_strategy.py)
  - notice that a theme change happened. Three ways to do that:

  - gtk-watcher (default) - GtkFileWatcherDetectionStrategy watches
    ~/.config/gtk-{3,4}.0/settings.ini for writes with inotify. Needs no
    D-Bus/portal plumbing to be working, just the watchdog package.
  - dbus - DBusEventDetectionStrategy listens for Plasma's D-Bus broadcast
    of the change via the xdg-desktop-portal "Settings" interface (the same
    interface Electron/Chromium use internally to learn about the system
    color scheme).
  - gsettings - GSettingsEventDetectionStrategy watches the
    org.gnome.desktop.interface color-scheme GSettings key directly, which
    KDE's GTK config integration keeps in sync with the Plasma theme.

- Fix (ThemeFixStrategy, see theme_fix_strategy.py) - nudge Electron apps
  into re-checking the theme. Two interchangeable ways to do that:

  - dconf (default) - DBusDirectSignalFixer emits a synthetic
    ca.desrt.dconf.Writer.Notify signal for an empty GNOME interface-theme
    key. Nothing about the real theme is ever read or written; the mere
    change *notification* is enough to make Electron/GTK apps re-check the
    system theme.
  - plasma - PlasmaColorSchemeFixer re-applies the *currently active* accent
    color with ``plasma-apply-colorscheme -a``. Re-issuing the color scheme
    by *name* doesn't work - plasma-apply-colorscheme refuses to set a
    scheme that's already active - but re-issuing the current accent color
    is accepted, and makes Plasma re-broadcast the color-scheme portal
    setting as a side effect, which is what actually does the nudging.

ThemeChangeFixer (theme_change_fixer.py) wires the two together: whenever
the chosen detection strategy reports a change, wait the chosen fix
strategy's apply_delay_seconds, then fire it - deduping an echo of our own
fix apart from a genuinely new change arriving in the same short window.

Requirements
------------
- The watchdog package for the default gtk-watcher detection strategy.
- gdbus (part of glib2) for the default dconf fix strategy.
- python-dbus and python-gobject only for the dbus / gsettings detection
  strategies - NOT imported unless one of those is selected (see
  theme_event_detection_strategy.py), so they don't need to be installed
  otherwise.
- plasma-apply-colorscheme and kreadconfig6/kreadconfig5 (part of
  plasma-workspace / kconfig) only for the plasma fix strategy.

Usage
-----
    ./electron-theme-fixer.py                                   # all defaults: gtk-watcher detection, dconf fix
    ./electron-theme-fixer.py --fix-strategy plasma              # KDE-Plasma-specific accent-color fix
    ./electron-theme-fixer.py --detection-strategy dbus          # D-Bus-portal-based detection
    ./electron-theme-fixer.py --detection-strategy gsettings     # GSettings-based detection

Run it in the background, e.g. as a systemd --user service - see
systemd/electron-theme-fix.service (or just run install.py).
"""

from __future__ import annotations

import argparse
import logging
import sys

from theme_change_fixer import ThemeChangeFixer
from theme_event_detection_strategy import ThemeEventDetectionStrategy
from theme_fix_strategy import DBusDirectSignalFixer, PlasmaColorSchemeFixer, ThemeFixStrategy

VERSION = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("electron-theme-fix")

# Both fix strategies only need subprocess/shutil (stdlib) - see
# theme_fix_strategy.py - so, unlike the detection strategies below, there's
# no reason not to import them unconditionally.
FIX_STRATEGIES: dict[str, type[ThemeFixStrategy]] = {
    "dconf": DBusDirectSignalFixer,
    "plasma": PlasmaColorSchemeFixer,
}

# Kept in sync with the branches in _load_detection_strategy() below - not a
# name -> class mapping like FIX_STRATEGIES, precisely because building one
# of those would require importing every strategy module unconditionally to
# populate it, defeating the point (see theme_event_detection_strategy.py).
DETECTION_STRATEGY_CHOICES = ["dbus", "gsettings", "gtk-watcher"]


def _load_detection_strategy(name: str) -> ThemeEventDetectionStrategy:
    """Import and instantiate the selected detection strategy.

    Deliberately the *only* place any of the three detection strategy
    modules gets imported, and only the one actually selected - each import
    is local to its branch so the other two modules (and whatever
    system-dependent libraries they need) are never touched. See the module
    docstring of theme_event_detection_strategy.py for why that matters.
    """
    if name == "dbus":
        from dbus_event_detection_strategy import DBusEventDetectionStrategy
        return DBusEventDetectionStrategy()
    if name == "gsettings":
        from gsettings_event_detection_strategy import GSettingsEventDetectionStrategy
        return GSettingsEventDetectionStrategy()
    if name == "gtk-watcher":
        from gtk_file_watcher_detection_strategy import GtkFileWatcherDetectionStrategy
        return GtkFileWatcherDetectionStrategy()
    raise ValueError(f"Unknown detection strategy: {name!r}")  # pragma: no cover - guarded by argparse choices


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Electron Theme Fixer for KDE Plasma - watches for light/dark "
        "theme changes and nudges Electron apps into picking them up.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "-f", "--fix-strategy", choices=sorted(FIX_STRATEGIES), default="dconf",
        help="How to nudge Electron apps into refreshing once a theme change is "
        "detected. 'dconf' (default): synthetic dconf change-notification. "
        "'plasma': re-apply the current accent color via plasma-apply-colorscheme.",
    )
    parser.add_argument(
        "-d", "--detection-strategy", choices=DETECTION_STRATEGY_CHOICES, default="gtk-watcher",
        help="How to detect that a theme change happened. 'gtk-watcher' "
        "(default): watch GTK settings.ini files with inotify. 'dbus': "
        "xdg-desktop-portal Settings D-Bus signal. 'gsettings': watch the "
        "GNOME color-scheme GSettings key.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()  # also handles --version/-h by printing and exiting

    fix_strategy: ThemeFixStrategy = FIX_STRATEGIES[args.fix_strategy]()
    fix_strategy.resolve_executables()

    detection_strategy = _load_detection_strategy(args.detection_strategy)
    detection_strategy.resolve_dependencies()

    fixer = ThemeChangeFixer(fix_strategy)
    detection_strategy.start(fixer.on_theme_changed)

    log.info(
        "Fixer ready (detection: %s, fix: %s).",
        type(detection_strategy).__name__, type(fix_strategy).__name__,
    )
    try:
        detection_strategy.run_forever()
    except KeyboardInterrupt:
        log.info("Stopping.")
    finally:
        detection_strategy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
