# Installer instructions / spec

This document is the design spec for `install.py`, kept in the repo so the
requirements are traceable to something other than git blame. If the
installer's behavior and this document ever disagree, that's a bug in one of
the two - fix whichever is wrong.

## Requirements (as given)

- MUST place files in the correct places.
- MUST support 2 install modes, selected with a flag: `--system`/`-s` or
  `--user`/`-u`. If neither is given, the installer MUST check whether it's
  allowed to install system-wide (high enough permissions) and fall back to
  user mode if not. **This fallback MUST NOT happen if a mode was selected
  explicitly via flag** - an explicit `--system` with insufficient
  permissions is an error, not a silent downgrade. The user MUST be informed
  about the fallback with a console warning.
- SYSTEM mode's default install directory is `/usr/share/electron-theme-fix`.
- USER mode's default install directory is `~/.local/share/electron-theme-fix`.
- The target directory MUST be configurable with `--install-dir`/`-d`.
- If the installer detects a conflicting setup (e.g. the target directory
  already exists), it MUST ask the user what to do: overwrite, change
  directory, or cancel.
- The installer MUST first check whether the app is already installed, by
  checking systemd units. This check CAN'T FAIL if the script doesn't have
  high enough permissions to read system units - if it can't read them,
  print a warning and continue by checking user-scope units instead.
- The installer MUST support uninstalling, either via a flag
  (`--uninstall`/`-U`) or via an `uninstall.sh` script placed in the app's
  install directory. That script CAN be a copy of the installer itself, but
  if it is, it MUST be hardcoded to always run in uninstall mode - even if
  the user renames the file - so detection cannot rely on the filename.
- BY DEFAULT the installer also installs the systemd service. This MUST be
  skippable with `--no-systemd`/`-nsd`. Skipping it MUST NOT skip the
  "already installed" check.
- The installer MUST detect whether the user is running KDE and has the
  required packages installed. If something is missing or the environment
  looks wrong, show a WARNING and prompt continue/cancel (y/n), and WAIT for
  the user's answer before continuing.
- The entry script (`electron-theme-fixer.py`, `fixer.py` in the original
  single-file layout) MUST support `--version`/`-v`, printing a version
  string (currently `2.0.0`).
- If the app is already installed (detected via a systemd unit or a plain
  filesystem check), the installer MUST identify the installed version and
  branch on it. Versions MUST be compared as plain dotted-integer versions
  (`X.Y.Z`); a version string that doesn't parse that way - on either side
  of the comparison - MUST be treated as invalid and normalized to `0.0.0`
  (i.e. it always compares as older than any valid current version, so it
  reads as an upgrade rather than blocking on an unrecognized version):
  - **Current newer than installed**: print `Upgrade possible!` and offer to
    upgrade - but the user MUST still be able to cancel the install.
  - **Same version, same target directory**: print `An existing installation
    found.` / `Reinstall?` (the original conflict-handling behavior).
  - **Same version, different target directory** (only reachable via the
    systemd check finding a match elsewhere): print `An existing systemd
    installation found at {directory}. Current installation directory:
    {target_directory}.` and offer `skip systemd` / `install systemd under a
    different name` / `cancel`.
  - **Current older than installed**: print `Newer version is already
    installed. Downgrade?` with a yes/no choice.
  - Whichever of the three branches above proceeds (upgrade, same-version
    reinstall, downgrade - all three operate on the *same* directory the
    current installation already lives in): **uninstall the current
    installation, then install the new version**, stopping the systemd
    service first if applicable. BY DEFAULT this uninstall runs via the
    **current installation**'s own `uninstall.sh` (not this installer's own
    uninstall logic), since a newer installer's assumptions may not match
    what an older version actually left behind. `--overwrite`/`-o` skips
    that uninstall step and overwrites the files in place instead (printing
    a WARNING that this may be problematic), and in that case the installer
    MUST daemon-reload and restart the systemd service afterward. The
    different-target-directory branch above is a distinct, coexisting-installs
    scenario, not covered by this uninstall-then-install step - neither of
    its own proceed-able choices (`skip systemd` / `install systemd under a
    different name`) uninstalls anything.
- After a successful install (whenever the systemd service was installed,
  i.e. not `--no-systemd` and not the "skip systemd" directory-mismatch
  choice), the installer MUST enable and launch the service automatically,
  then verify it actually came up - printing an error (and exiting non-zero)
  if it didn't, instead of just telling the user the command to run.

## Design notes (how the requirements are actually implemented)

- **Permission check for system mode**: `os.geteuid() == 0`. Simple, and
  matches how `/usr/share` and `/etc/systemd/system` are normally writable
  only by root anyway (via `sudo`).
- **"Already installed" check**: checks for the unit file directly at
  `/etc/systemd/system/electron-theme-fix.service` (system scope) and
  `~/.config/systemd/user/electron-theme-fix.service` (user scope)
  independently, each wrapped so an `OSError` (e.g. no permission to stat the
  system path) degrades to a printed warning rather than a crash, instead of
  blocking the other scope's check. Runs before any install/uninstall action,
  unconditionally (even with `--no-systemd`).
- **`uninstall.sh`**: generated by copying `install.py` itself into the
  install directory and rewriting two lines at the top of the copy:
  `_FORCE_UNINSTALL = True` (so it always runs as an uninstaller regardless
  of what it's renamed to) and `_BAKED_INSTALL_DIR` / `_BAKED_MODE` (so it
  knows what to remove without having to re-detect anything).
- **Systemd unit installation does not `enable --now` automatically.** It
  writes the unit file and runs `daemon-reload`, then prints the exact
  `systemctl enable --now ...` command for the user to run. Not
  auto-starting a background D-Bus listener as a side effect of running an
  installer seemed like the safer default; this can change if that's not
  what's wanted.
- The systemd unit content is generated from a template in `install.py`
  (not the static `systemd/electron-theme-fix.service` file in the
  repo, which stays as a plain reference/example for manual installs) so
  `ExecStart` always matches wherever `electron-theme-fixer.py` actually
  ends up.
- **The installer bundles the whole `src/` directory, not a single file.**
  `install.py` now installs `src/electron-theme-fixer.py` (the entry point)
  together with every strategy module it lazily imports
  (`theme_fix_strategy.py`, `theme_change_fixer.py`,
  `theme_event_detection_strategy.py`, and the three
  `*_event_detection_strategy.py` files) via a directory copy
  (`copy_source_tree()`), not a single `shutil.copy2()`. Nothing is filtered
  out based on which optional dependency is present at install time - it's
  each detection strategy module's own lazy import (only reached if that
  strategy is actually selected at *run* time, see
  `src/theme_event_detection_strategy.py`) that keeps an install usable
  without e.g. `watchdog` present, not anything the installer decides.
- **Version reading never executes anything.** Both the "current" version
  (`src/electron-theme-fixer.py` next to `install.py`) and an existing
  install's version are read by regexing the *source text* for a
  `VERSION = "..."` line - never by running or importing the script. This
  means a version check can't be broken by a missing optional dependency
  (python-dbus/python-gobject/watchdog) in either script, and never risks
  executing arbitrary code from an existing install just to ask its version.
  `electron-theme-fixer.py` mirrors the spirit of this on the run side: each
  detection strategy's `dbus`/`gi`/`watchdog` import is local to the branch
  that selected it, so `./electron-theme-fixer.py --version` works
  regardless of which of those are installed.
- **Existing-install detection** (`detect_existing_install()`) checks the
  *target* directory for an `electron-theme-fixer.py` first (a plain
  filesystem check); only if that's empty does it fall back to parsing
  `ExecStart=` out of the current mode's systemd unit (if any) to find where
  a previous install put its files. The directory-mismatch branch is
  therefore only reachable via the systemd path, matching the spec's
  "(systemd check has found a match)" parenthetical.
- **"Install systemd under a different name"** prompts for a new unit
  filename (suggesting `{APP_NAME}-2.service`), re-prompting if that name is
  also already taken, and installs using that name instead of the default -
  both units then coexist untouched.
- **Auto-launch** (`enable_and_start_service()`) runs `systemctl enable` then
  `systemctl restart` (not `start`) - `restart` is used unconditionally
  because it correctly handles both "not running yet" (behaves like `start`)
  and "already running under the old files" (the `--overwrite` path), so the
  same call works for a fresh install, a reinstall, and an overwrite alike.
  `enable` failing is only a warning (the service can still run now even if
  it won't auto-start next login); `restart` failing, or the unit not
  reporting `active` within a few short polls afterward, is a hard error -
  printed to stderr with a `journalctl` hint, and the installer exits 1.
  (`Type=simple` is marked active as soon as the process forks, so a
  crash-on-startup shows up within the poll window rather than needing a
  long timeout.)

## Testing notes

- No sudo is available in the dev/test environment this was built in, so
  system-mode installs were never actually exercised end-to-end - only the
  permission-check/error/fallback branches (which correctly refuse to write
  anything without root).
- User-mode installs were tested against a throwaway `$HOME` so that nothing
  under the real `~/.local/share` or `~/.config/systemd/user` was touched.
- Whenever a run under test needed to prove what a systemd unit file's
  contents would look like, that content was captured into
  `SYSTEMD_WRITES.txt` instead of being left behind under a real systemd
  search path, and the throwaway `$HOME` used to produce it was deleted
  afterward. `SYSTEMD_WRITES.txt` is a testing artifact, not part of the
  installed product.
- The version-comparison logic (upgrade/reinstall/downgrade/directory
  mismatch/unknown-version fallback, both proceed and cancel for each) was
  exercised end-to-end against throwaway `$HOME`s using a second, patched
  copy of the repo with `fixer.py`'s `VERSION` changed to `0.9.0`, alternating
  which copy's `install.py` ran the "current" side of the comparison. Every
  branch correctly left the real system untouched.
- **Important caveat found while testing auto-launch**: overriding `$HOME`
  fully sandboxes plain file writes (everything under `USER_DEFAULT_INSTALL_DIR`
  / `USER_UNIT_DIR`, both derived from `Path.home()`), but does **not**
  reliably sandbox `systemctl --user` calls - those talk to the real,
  already-running per-login user manager over D-Bus, which is not obviously
  scoped by a child process's `$HOME`. A throwaway-`$HOME` test run of
  `enable_and_start_service()` was seen to actually enable and start a real
  service on the real system once, and could not be made to repeat on a
  clean retry (an isolated retry correctly failed with "Unit not found," as
  expected against a fake `$HOME`). Because this couldn't be pinned down or
  reliably reproduced, `enable_and_start_service()`'s logic was instead
  verified with `install.systemctl` monkeypatched to return canned results
  (success, never-becomes-active, fails fast on `is-active: failed`, restart
  itself fails, `systemctl` missing entirely) rather than by further calling
  it for real. If you touch this function again: do not trust a throwaway
  `$HOME` alone to make real `systemctl --user enable/restart` calls safe to
  test with; verify real system state before and after regardless.
- **This caveat is not hypothetical - it bit the src/-layout re-test.** When
  `install.py` was updated to bundle `src/` (multiple files, `ENTRY_FILENAME
  = "electron-theme-fixer.py"`) instead of a single `fixer.py`, one retest
  command invoked a generated `uninstall.sh` directly without re-exporting
  the throwaway `HOME=` prefix used for every other command in that session.
  Since `uninstall.sh` is a plain copy of `install.py` run via its own
  shebang, it picked up the real `Path.home()`, and `remove_unit()` deleted
  a real, genuinely-in-use `~/.config/systemd/user/electron-theme-fix.service`
  (confirmed pre-existing via `journalctl --user -u electron-theme-fix`
  showing real start/stop cycles from earlier the same day). The install
  directory itself was untouched (`_BAKED_INSTALL_DIR` was still the fake
  path), and the unit had never been `enable`d (no `graphical-session.target.wants`
  symlink existed), so recovery was just regenerating that one file from
  `install.UNIT_TEMPLATE` with `ExecStart` pointed back at the real
  `~/.local/share/electron-theme-fix/fixer.py` and running `daemon-reload` -
  but this is exactly the "verify real system state before and after
  regardless" failure mode the caveat above already named. Concretely: every
  command in a throwaway-`$HOME` test session needs the `HOME=` prefix
  individually, including ones that just invoke a generated script by path -
  a shebang line does not inherit a prefix set on a sibling command, and
  nothing about the *installer's* sandboxing catches the omission.
