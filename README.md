# Electron Theme Fixer for KDE Plasma

This is a workaround for [electron/electron#48736](https://github.com/electron/electron/issues/48736)
("Regression: v39 incorrectly resolves the system theme (light/dark) at
runtime and also calculates `nativeTheme.shouldUseDarkColors` incorrectly at
runtime") — Electron picks up the *previous* system theme instead of the
current one after a runtime light/dark switch on KDE Plasma/Wayland.

## How it works

Plasma broadcasts light/dark switches over D-Bus through the
`org.freedesktop.portal.Settings` interface (`SettingChanged` signal on
`org.freedesktop.appearance` / `color-scheme`) — the same interface
Electron/Chromium apps use to learn the system color scheme.

This script listens for that signal and, shortly after a change, re-applies
the *currently active* accent color via `plasma-apply-colorscheme -a`.
(Re-issuing the color scheme by *name* doesn't work - `plasma-apply-colorscheme`
refuses to set a scheme that's already active - but re-issuing the current
accent color is accepted, and forces the same fresh theme-changed notification
that gets Electron apps to actually repaint with the correct theme.)

## Other desktop environments

This script relies on KDE Plasma-specific plumbing (`plasma-apply-colorscheme`,
`kreadconfig`) and **does not work on GNOME**.

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

- `python-dbus` and `python-gobject` (GLib main loop)
- `plasma-apply-colorscheme` (ships with `plasma-workspace`)
- `kreadconfig6` or `kreadconfig5` (ships with `kconfig` / `plasma-workspace`)

On Arch/CachyOS:

```sh
sudo pacman -S python-dbus python-gobject plasma-workspace
```

## Try it out first

```sh
./fixer.py
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

This copies `fixer.py` into the install directory (`/usr/share/electron-theme-fix`
system-wide, `~/.local/share/electron-theme-fix` per-user, by default),
installs a systemd unit pointed at it, and drops a matching `uninstall.sh`
next to the script. It also checks you're actually on KDE Plasma with the
required tools available, and warns and asks before continuing if not.

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
[INSTALLER_INSTRUCTIONS.md](INSTALLER_INSTRUCTIONS.md).

Prefer to skip the installer entirely? Add `fixer.py` to Plasma's autostart
instead (System Settings → Autostart → Add Script).

## Tuning

Both knobs live in the `Consts` class in `fixer.py`
(currently set low, to `0.1`s, which works well on my machine - but for reliability should be set to 0.5s or even 1.0s):

- `APPLY_DELAY_SECONDS` — delay between detecting a theme change and applying
  the fix. Increase it if Electron apps are still catching the fix
  mid-transition.
- `SUPPRESS_WINDOW_SECONDS` — after the fix is applied, `plasma-apply-colorscheme`
  re-broadcasts the same `color-scheme` change as a side effect, which would
  otherwise re-trigger the fix in an infinite loop. Signals arriving within
  this window after a fix are ignored as an echo of our own change. Keep it
  at least as large as `APPLY_DELAY_SECONDS`.
