"""ThemeFixStrategy interface and implementations.

A ThemeFixStrategy is the "nudge Electron apps into refreshing" half of the
fixer (see theme_event_detection_strategy.py for the other half - "notice a
theme change happened in the first place"). Exactly one strategy is active
per run, chosen in electron-theme-fixer.py's main() via --fix-strategy.

Both implementations here only need subprocess/shutil (stdlib), so - unlike
the detection strategies - there's no reason to split them into separate
files or import them lazily; see the module docstring of
theme_event_detection_strategy.py for why *that* split exists.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger("electron-theme-fix")


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


class ThemeFixStrategy(ABC):
    """Common interface for a "nudge Electron apps into refreshing" method."""

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
    -a`` (see the module docstring of the top-level fixer.py for why that's
    what actually nudges Electron apps). KDE-Plasma-specific: needs
    kreadconfig and plasma-apply-colorscheme.
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
