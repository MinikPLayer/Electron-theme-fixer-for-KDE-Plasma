# Electron Theme Fixer for KDE Plasma

This is a workaround for [electron/electron#48736](https://github.com/electron/electron/issues/48736)
("Regression: v39 incorrectly resolves the system theme (light/dark) at
runtime and also calculates `nativeTheme.shouldUseDarkColors` incorrectly at
runtime") — Electron picks up the *previous* system theme instead of the
current one after a runtime light/dark switch on KDE Plasma/Wayland.

## AI Warning

This repo contains code that is partially AI generated with heavy human guidance.
Implemented solutions were my ideas, but the final code was developed with heavy AI usage - even if AI was only used as a tool to implement a previously defined algorithm (instead of letting it generate the architecture freely).
This was by design, as this project was meant to be a training exercise for me on how to use AI effectively.
If this bothers You - skip this repo and I totally understand not everyone is okay with that.

## How it works

The fix has two independent halves, each with a choice of interchangeable
strategies - pick one of each with a flag, or just take the defaults:

- **Detection** (`ThemeEventDetectionStrategy`) - notice that the theme
  changed:
  - **`gtk-watcher` (default)** - watches `~/.config/gtk-3.0` and
    `~/.config/gtk-4.0` for writes to `settings.ini` with inotify. KDE's GTK
    config integration rewrites those whenever the Plasma theme changes, to
    keep native GTK apps in sync - this strategy just watches for that
    directly. No D-Bus/portal plumbing required, just the `watchdog` package.
  - **`dbus`** - listens for Plasma's D-Bus broadcast of the change via the
    xdg-desktop-portal `Settings` interface (`SettingChanged` signal on
    `org.freedesktop.appearance` / `color-scheme`) - the same interface
    Electron/Chromium apps use internally to learn the system color scheme.
  - **`gsettings`** - watches the `org.gnome.desktop.interface` /
    `color-scheme` GSettings key directly, which KDE's GTK config
    integration also keeps in sync with the Plasma theme.
- **Fix** (`ThemeFixStrategy`) - nudge Electron apps into re-checking the
  theme once a change was detected:
  - **`dconf` (default)** - emits a synthetic `ca.desrt.dconf.Writer.Notify`
    signal for an empty GNOME interface-theme key. Nothing about the real
    theme is ever read or written; the mere change *notification* is enough
    to make Electron/GTK apps re-check the system theme.
  - **`plasma`** - re-applies the *currently active* accent color via
    `plasma-apply-colorscheme -a`. (Re-issuing the color scheme by *name*
    doesn't work - `plasma-apply-colorscheme` refuses to set a scheme
    that's already active - but re-issuing the current accent color is
    accepted, and forces the same fresh theme-changed notification that
    gets Electron apps to actually repaint with the correct theme.)

A third piece, `ThemeChangeFixer`, wires the two together: whenever the
chosen detection strategy reports a change, wait the chosen fix strategy's
tuned delay, then fire it - deduping an echo of our own fix apart from a
genuinely new change arriving in the same short window.

Pick a non-default pair with flags:

```sh
src/electron-theme-fixer.py --detection-strategy dbus --fix-strategy plasma
src/electron-theme-fixer.py -d gsettings -f dconf
```

## Project layout

```
src/
  electron-theme-fixer.py            entry point - CLI, wiring
  theme_event_detection_strategy.py  ThemeEventDetectionStrategy interface
  dbus_event_detection_strategy.py   DBusEventDetectionStrategy
  gsettings_event_detection_strategy.py  GSettingsEventDetectionStrategy
  gtk_file_watcher_detection_strategy.py GtkFileWatcherDetectionStrategy
  theme_fix_strategy.py              ThemeFixStrategy interface + both fixers
  theme_change_fixer.py              detect → dedupe → delay → fire glue
```

Each detection strategy lives in its own file and is imported lazily - only
the one actually selected (see `_load_detection_strategy()` in
`electron-theme-fixer.py`) - so e.g. the `watchdog` package doesn't need to
be installed unless you're actually using `--detection-strategy gtk-watcher`,
and likewise for `python-dbus`/`python-gobject` and `dbus`/`gsettings`.

`fixer.py` at the repo root is the original single-file prototype (D-Bus
detection only) this was built up from; it's kept around for reference but
isn't what gets installed - `install.py` installs the `src/` layout above.

## Other desktop environments

Every detection strategy here relies on some piece of KDE-Plasma-specific
plumbing (the xdg-desktop-portal `Settings` implementation for `dbus`, or
KDE's GTK config integration keeping GSettings/`settings.ini` in sync for
`gsettings`/`gtk-watcher`) and has only been built and tested against Plasma
- none of them are verified to work on GNOME, even though the default fix
strategy happens to use a GNOME-native dconf signal. `--fix-strategy plasma`
additionally needs KDE-Plasma-specific tools (`plasma-apply-colorscheme`,
`kreadconfig`).

On GNOME, GTK3 apps (including Electron) revert to the old theme almost
instantly after a light/dark switch, because GNOME doesn't update the
`gtk-theme` gsetting by itself. The GNOME-side fix would be a different
script entirely - one that watches for the theme change and then forces GTK3 theme change by running something like:

```sh
gsettings set org.gnome.desktop.interface gtk-theme "adw-gtk3-dark"
# or, back in light mode:
gsettings set org.gnome.desktop.interface gtk-theme "adw-gtk3"
```

That's outside the scope of this project.

## Requirements

- `watchdog` for the default `gtk-watcher` detection strategy.
- `gdbus` (ships with `glib2`) for the default `dconf` fix strategy.
- `python-dbus` and `python-gobject` (GLib main loop) only for the `dbus` /
  `gsettings` detection strategies - not required otherwise (see "Project
  layout" above for why).
- `plasma-apply-colorscheme` and `kreadconfig6`/`kreadconfig5` (ship with
  `plasma-workspace` / `kconfig`) only if using `--fix-strategy plasma`.

On Arch/CachyOS:

```sh
sudo pacman -S python-watchdog glib2
# only if you'll use --detection-strategy dbus/gsettings or --fix-strategy plasma:
sudo pacman -S python-dbus python-gobject plasma-workspace
```

## Try it out first

```sh
./src/electron-theme-fixer.py
```

Run it in the foreground to confirm it picks up theme switches (toggle
dark/light mode in System Settings and watch the log output) before
installing it to run automatically.

## Installing

```sh
./install.py                      # auto: system-wide if run as root, otherwise per-user
./install.py --system             # force system-wide install (needs sudo/root)
./install.py --user               # force a per-user install
./install.py --install-dir DIR    # install somewhere other than the default
./install.py --no-systemd         # copy the files but skip the systemd service
```

This copies the whole `src/` directory into the install directory
(`/usr/share/electron-theme-fix` system-wide, `~/.local/share/electron-theme-fix`
per-user, by default), installs a systemd unit pointed at `electron-theme-fixer.py`
there, and drops a matching `uninstall.sh` next to it. It also checks you're
actually on KDE Plasma with the required tools available for the *default*
strategies (`gtk-watcher` detection, `dconf` fix), and warns and asks before
continuing if not - tools only needed for a non-default strategy
(`plasma-apply-colorscheme`/`kreadconfig`, `python-dbus`/`python-gobject`)
are a soft note instead, not a blocker.

Unless `--no-systemd` was passed, the installer also enables and starts the
service itself and verifies it actually came up - if it didn't, it prints an
error (with a `journalctl` command to check why) and exits non-zero instead
of leaving you to discover that later.

Check status/logs with:

```sh
systemctl [--user] status electron-theme-fix.service
journalctl [--user] -u electron-theme-fix.service -f
```

(drop `--user` for a system-wide install)

To uninstall:

```sh
<install-dir>/uninstall.sh
# or, from this repo:
./install.py --uninstall
```

Full installer behavior (modes, conflict handling, flags) is documented in
[AI_DOCS/INSTALLER_INSTRUCTIONS.md](AI_DOCS/INSTALLER_INSTRUCTIONS.md).

Prefer to skip the installer entirely? Add `electron-theme-fixer.py` to
Plasma's autostart instead (System Settings → Autostart → Add Script) -
just make sure the whole `src/` directory travels with it, since it imports
its strategy modules as siblings.

## Tuning

- `apply_delay_seconds` — a class attribute on each `ThemeFixStrategy` in
  `src/theme_fix_strategy.py` (`DBusDirectSignalFixer.apply_delay_seconds = 0.2`,
  `PlasmaColorSchemeFixer.apply_delay_seconds = 1.0`) — the delay between
  detecting a theme change and applying that strategy's fix. Increase it if
  Electron apps are still catching the fix mid-transition; each strategy is
  tuned independently since they need different margins.
- `Consts.SUPPRESS_WINDOW_SECONDS` in `src/theme_change_fixer.py` — how long
  a fired fix's (value, timestamp) is remembered, to tell an echo of our own
  fix apart from a genuinely new theme switch arriving in the same window.
  Whether a given strategy *pairing* actually causes such an echo depends on
  whether the fix's action happens to touch whatever the detection strategy
  is watching:
  - `dconf` fix + `gsettings` detection **does** echo - the fix's synthetic
    notification is emitted directly on the same GSettings/dconf key the
    detection strategy watches.
  - `dconf` fix + `dbus`/`gtk-watcher` detection does not - the synthetic
    dconf notification never touches the portal `SettingChanged` signal or
    the GTK `settings.ini` files those two watch.
  - `plasma` fix is a real, full theme reapplication, so it's expected to
    echo under all three detection strategies - this is the whole reason
    `SUPPRESS_WINDOW_SECONDS` exists in the first place, back when `dbus`
    detection + `plasma` fix was the only pairing this project had;
    `gsettings`/`gtk-watcher` detection follow the same reasoning but
    weren't independently re-exercised against the `plasma` fix.

  Either way it's a harmless no-op for a pairing that doesn't echo. Keep it
  comfortably larger than the *largest* `apply_delay_seconds` you use - the
  window is measured from when the fix actually fires, not from detection,
  but it still needs headroom for the echo's own round trip on top of that.
