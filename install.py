#!/usr/bin/env python3
"""Installer / uninstaller for Electron Theme Fixer for KDE Plasma.

See INSTALLER_INSTRUCTIONS.md for the full spec this implements.

Usage:
    ./install.py [--system | --user] [--install-dir DIR] [--no-systemd] [--overwrite]
    ./install.py --uninstall [--system | --user] [--install-dir DIR]
    ./install.py --version

Uninstalling later doesn't require this file: a self-uninstalling copy is
dropped into the install directory as uninstall.sh at install time.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# --- Uninstaller self-configuration -----------------------------------------
# When this script copies itself into the install directory as uninstall.sh,
# it rewrites the three lines below in the copy, so that copy always runs in
# uninstall mode - regardless of what the file is renamed to - and already
# knows what to remove without re-detecting anything. Do not reformat these
# three lines; write_uninstaller() matches them verbatim.
_FORCE_UNINSTALL = False
_BAKED_MODE = ""  # "system" or "user", filled in on the generated copy
_BAKED_INSTALL_DIR = ""  # absolute path, filled in on the generated copy

APP_NAME = "electron-theme-fix"
SERVICE_NAME = f"{APP_NAME}.service"
SCRIPT_FILENAME = "fixer.py"
SOURCE_SCRIPT = Path(__file__).resolve().parent / SCRIPT_FILENAME

SYSTEM_DEFAULT_INSTALL_DIR = Path("/usr/share") / APP_NAME
USER_DEFAULT_INSTALL_DIR = Path.home() / ".local" / "share" / APP_NAME

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

KREADCONFIG_CANDIDATES = ("kreadconfig6", "kreadconfig5")

UNIT_TEMPLATE = """[Unit]
Description=Fix Electron apps lagging one theme change behind Plasma
After=plasma-plasmashell.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
"""


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def warn(message: str) -> None:
    eprint(f"WARNING: {message}")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def ask_choice(prompt: str, choices: dict) -> str:
    print(prompt)
    for key, label in choices.items():
        print(f"  [{key}] {label}")
    valid = set(choices)
    while True:
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            return "x" if "x" in valid else next(iter(valid))
        if answer in valid:
            return answer
        print(f"Please enter one of: {', '.join(sorted(valid))}")


# --- Version handling --------------------------------------------------------
# Versions are read by regexing the *source text* of a fixer.py, never by
# executing/importing it. That means version checks work even against a
# fixer.py whose dependencies (python-dbus/python-gobject) aren't installed,
# and never risk running someone else's arbitrary code just to ask "what
# version are you".

_VERSION_RE = re.compile(r'^VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', re.MULTILINE)
_EXEC_START_RE = re.compile(r'^ExecStart=(.+)$', re.MULTILINE)


def read_version_from_script(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def parse_exec_start(unit_path: Path) -> Path | None:
    try:
        text = unit_path.read_text()
    except OSError:
        return None
    match = _EXEC_START_RE.search(text)
    return Path(match.group(1).strip()) if match else None


def _version_tuple(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def compare_versions(a: str, b: str) -> int | None:
    """-1 if a<b, 0 if a==b, 1 if a>b, None if either can't be parsed as
    dotted integers (e.g. "1.0.0")."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    if ta is None or tb is None:
        return None
    return (ta > tb) - (ta < tb)


CURRENT_VERSION = read_version_from_script(SOURCE_SCRIPT)


@dataclass
class ExistingInstall:
    directory: Path
    version: str | None
    via: str  # "filesystem" or "systemd"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or uninstall Electron Theme Fixer for KDE Plasma. "
        "See INSTALLER_INSTRUCTIONS.md for the full behavior spec.",
    )
    parser.add_argument(
        "-v", "--version", action="version",
        version=f"%(prog)s (bundles {SCRIPT_FILENAME} {CURRENT_VERSION or 'unknown'})",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-s", "--system", action="store_true",
        help=f"Install system-wide (default dir: {SYSTEM_DEFAULT_INSTALL_DIR}). Requires root.",
    )
    mode_group.add_argument(
        "-u", "--user", action="store_true",
        help=f"Install for the current user only (default dir: {USER_DEFAULT_INSTALL_DIR}).",
    )
    parser.add_argument(
        "-d", "--install-dir", type=Path, default=None,
        help="Override the install directory.",
    )
    parser.add_argument(
        "-nsd", "--no-systemd", action="store_true",
        help="Don't install the systemd service (the already-installed check still runs).",
    )
    parser.add_argument(
        "-o", "--overwrite", action="store_true",
        help="When replacing an existing install, overwrite its files in place instead of "
        "running its uninstall.sh first. Faster, but can leave stale files behind or hit a "
        "running service mid-swap; daemon-reload + restart run afterward to compensate.",
    )
    # NOTE: -u is already taken by --user (see INSTALLER_INSTRUCTIONS.md,
    # "Deviations from the literal spec"), so --uninstall uses -U instead.
    parser.add_argument(
        "-U", "--uninstall", action="store_true",
        help="Uninstall instead of installing.",
    )
    return parser.parse_args(argv)


def can_install_system_wide() -> bool:
    return os.geteuid() == 0


def unit_path_for(mode: str, service_name: str = SERVICE_NAME) -> Path:
    return (SYSTEM_UNIT_DIR if mode == "system" else USER_UNIT_DIR) / service_name


def check_already_installed() -> tuple[Path | None, Path | None]:
    """Quick heads-up check for existing systemd units, without letting a
    permissions problem on the system side stop the user-side check from
    running. This is just an informational banner printed up front; the
    install path's actual upgrade/downgrade/conflict handling is done by the
    more thorough detect_existing_install()."""
    system_hit: Path | None = None
    try:
        if unit_path_for("system").exists():
            system_hit = unit_path_for("system")
    except OSError as exc:
        warn(
            f"Could not check for a system-wide install ({unit_path_for('system')}: {exc}); "
            "continuing with the user-scope check only."
        )

    user_hit: Path | None = None
    try:
        if unit_path_for("user").exists():
            user_hit = unit_path_for("user")
    except OSError as exc:
        warn(f"Could not check for a user-scope install ({unit_path_for('user')}: {exc}).")

    return system_hit, user_hit


def detect_existing_install(mode: str, target_dir: Path) -> ExistingInstall | None:
    """Look for a pre-existing install relevant to this run: first a plain
    filesystem check against the target directory itself, then (if that
    turns up nothing) the systemd unit for this mode's scope, whose
    ExecStart may point somewhere else entirely."""
    target_script = target_dir / SCRIPT_FILENAME
    if target_dir.exists() and target_script.exists():
        return ExistingInstall(
            directory=target_dir,
            version=read_version_from_script(target_script),
            via="filesystem",
        )

    unit_path = unit_path_for(mode)
    try:
        if not unit_path.exists():
            return None
    except OSError as exc:
        warn(f"Could not inspect existing systemd unit {unit_path}: {exc}")
        return None

    exec_start = parse_exec_start(unit_path)
    if exec_start is None:
        warn(f"Found {unit_path} but couldn't parse its ExecStart= line.")
        return None

    return ExistingInstall(
        directory=exec_start.parent,
        version=read_version_from_script(exec_start),
        via="systemd",
    )


def check_environment_or_prompt() -> None:
    problems = []

    current_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    session_desktop = os.environ.get("XDG_SESSION_DESKTOP", "")
    if "KDE" not in current_desktop.upper() and "KDE" not in session_desktop.upper():
        problems.append(
            "This doesn't look like a KDE Plasma session "
            f"(XDG_CURRENT_DESKTOP={current_desktop!r}, XDG_SESSION_DESKTOP={session_desktop!r}). "
            "This tool only works on KDE Plasma."
        )

    if not shutil.which("plasma-apply-colorscheme"):
        problems.append(
            "plasma-apply-colorscheme was not found in PATH "
            "(usually part of the 'plasma-workspace' package)."
        )

    if not any(shutil.which(c) for c in KREADCONFIG_CANDIDATES):
        problems.append(
            "Neither kreadconfig6 nor kreadconfig5 was found in PATH "
            "(usually part of 'kconfig' / 'plasma-workspace')."
        )

    for module, package in (("dbus", "python-dbus"), ("gi", "python-gobject")):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"Python module '{module}' is not importable (install '{package}').")

    if not problems:
        return

    warn("Potential environment issues detected:")
    for problem in problems:
        eprint(f"  - {problem}")
    if not ask_yes_no("Continue installing anyway? [y/N] ", default=False):
        print("Installation cancelled.")
        sys.exit(1)


def resolve_mode(args: argparse.Namespace) -> str:
    if args.system:
        if not can_install_system_wide():
            eprint("ERROR: --system was requested but this script is not running as root.")
            eprint("Re-run with sudo, or drop --system to install for the current user instead.")
            sys.exit(1)
        return "system"

    if args.user:
        return "user"

    # No mode flag given: try system-wide, fall back to user with a warning.
    if can_install_system_wide():
        return "system"

    warn(
        "No install mode specified and this script is not running as root; "
        "falling back to a user-scope install. Pass --system (with sudo) to "
        "install system-wide, or --user to silence this warning."
    )
    return "user"


def resolve_install_dir(mode: str, args: argparse.Namespace) -> Path:
    if args.install_dir:
        return args.install_dir.expanduser().resolve()
    return SYSTEM_DEFAULT_INSTALL_DIR if mode == "system" else USER_DEFAULT_INSTALL_DIR


def resolve_conflict(install_dir: Path) -> Path:
    """Generic fallback for when something is sitting at the target directory
    but detect_existing_install() couldn't confidently identify it as a
    recognizable install of this app (e.g. version unreadable)."""
    while True:
        if not install_dir.exists():
            return install_dir

        choice = ask_choice(
            f"Target directory already exists: {install_dir}",
            {
                "o": "Overwrite (remove and reinstall)",
                "c": "Change install directory",
                "x": "Cancel",
            },
        )
        if choice == "o":
            return install_dir
        if choice == "c":
            new_dir = input("New install directory: ").strip()
            if new_dir:
                install_dir = Path(new_dir).expanduser().resolve()
            continue
        print("Installation cancelled.")
        sys.exit(1)


def systemctl(mode: str, *args: str) -> subprocess.CompletedProcess | None:
    cmd = (["systemctl"] if mode == "system" else ["systemctl", "--user"]) + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None


def daemon_reload(mode: str) -> None:
    result = systemctl(mode, "daemon-reload")
    if result is None or result.returncode != 0:
        warn("Could not run 'systemctl daemon-reload'; you may need to run it manually.")


def enable_and_start_service(mode: str, service_name: str) -> bool:
    """Enable + (re)start the service, then verify it actually came up.
    Prints an error and returns False if it didn't; used for both a plain
    fresh install and the --overwrite path (restart works whether the unit
    was already running, freshly loaded, or not running at all)."""
    print(f"Starting {service_name}...")

    enable_result = systemctl(mode, "enable", service_name)
    if enable_result is None or enable_result.returncode != 0:
        warn(f"Could not enable {service_name} (it may not start automatically at login); trying to start it anyway.")

    restart_result = systemctl(mode, "restart", service_name)
    if restart_result is None:
        eprint(f"ERROR: systemctl not found; could not start {service_name}.")
        return False
    if restart_result.returncode != 0:
        eprint(f"ERROR: Failed to start {service_name}.")
        detail = restart_result.stderr.strip() or restart_result.stdout.strip()
        if detail:
            eprint(f"  {detail}")
        return False

    # Type=simple is marked active as soon as the process is forked, so a
    # crash-on-startup (e.g. a missing dependency) shows up within a couple
    # of restart attempts (Restart=on-failure / RestartSec=2) rather than
    # instantly - poll briefly instead of checking only once.
    state = "unknown"
    for _ in range(4):  # ~2s total
        time.sleep(0.5)
        status = systemctl(mode, "is-active", service_name)
        state = status.stdout.strip() if status else "unknown"
        if state == "active":
            print(f"{service_name} is active.")
            return True
        if state == "failed":
            break  # no point waiting out the rest of the timeout

    eprint(f"ERROR: {service_name} does not appear to be running (status: {state}).")
    scope = "--user " if mode == "user" else ""
    eprint(f"Check logs with: journalctl {scope}-u {service_name} -n 20 --no-pager")
    return False


def stop_and_disable(mode: str, service_name: str) -> None:
    systemctl(mode, "stop", service_name)
    systemctl(mode, "disable", service_name)


def remove_unit(mode: str, service_name: str) -> None:
    path = unit_path_for(mode, service_name)
    try:
        if path.exists():
            path.unlink()
            print(f"  Removed systemd unit: {path}")
            daemon_reload(mode)
    except OSError as exc:
        warn(f"Could not remove systemd unit {path}: {exc}")


def write_uninstaller(install_dir: Path, mode: str) -> Path:
    # Line-prefix (not bare substring) matching, so this doesn't get confused
    # by the replacements dict below quoting these same three lines as data -
    # that data lives indented inside a dict literal, so it never *starts* a
    # line with these prefixes the way the real assignments at file scope do.
    installer_source = Path(__file__).resolve()
    lines = installer_source.read_text().splitlines(keepends=True)

    replacements = {
        "_FORCE_UNINSTALL = False": "_FORCE_UNINSTALL = True",
        '_BAKED_MODE = ""': f"_BAKED_MODE = {mode!r}",
        '_BAKED_INSTALL_DIR = ""': f"_BAKED_INSTALL_DIR = {str(install_dir)!r}",
    }

    def rewrite(line: str) -> str:
        stripped = line.rstrip("\n")
        for prefix, new_prefix in replacements.items():
            if stripped.startswith(prefix):
                return new_prefix + stripped[len(prefix):] + "\n"
        return line

    new_lines = [rewrite(line) for line in lines]

    for prefix in replacements:
        matches = sum(1 for line in lines if line.rstrip("\n").startswith(prefix))
        if matches != 1:
            raise RuntimeError(
                f"Refusing to generate uninstall.sh: expected exactly one line "
                f"starting with {prefix!r} in {installer_source}, found {matches}."
            )

    content = "".join(new_lines)

    dest = install_dir / "uninstall.sh"
    dest.write_text(content)
    dest.chmod(0o755)
    return dest


def install_systemd_unit(mode: str, install_dir: Path, service_name: str) -> None:
    unit_path = unit_path_for(mode, service_name)
    exec_start = str(install_dir / SCRIPT_FILENAME)

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(UNIT_TEMPLATE.format(exec_start=exec_start))
    print(f"  Systemd unit: {unit_path}")
    daemon_reload(mode)


def perform_install(mode: str, install_dir: Path, install_systemd: bool, service_name: str = SERVICE_NAME) -> Path:
    print(f"Installing to {install_dir} ({mode} mode)...")

    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    dest_script = install_dir / SCRIPT_FILENAME
    shutil.copy2(SOURCE_SCRIPT, dest_script)
    dest_script.chmod(0o755)
    print(f"  Script: {dest_script}")

    uninstaller = write_uninstaller(install_dir, mode)
    print(f"  Uninstaller: {uninstaller}")

    if install_systemd:
        install_systemd_unit(mode, install_dir, service_name)
    else:
        print("  Systemd unit: skipped (--no-systemd)")

    print("\nInstall complete.")
    print(f"To uninstall later, run: {uninstaller}")
    return uninstaller


# --- Existing-install handling (upgrade / reinstall / downgrade / conflict) --

def teardown_old_install(existing: ExistingInstall, mode: str, service_name: str, overwrite: bool) -> None:
    if overwrite:
        warn(
            "--overwrite passed: skipping the existing installation's own uninstall step and "
            "overwriting its files in place instead. This can leave stale files behind, or briefly "
            "confuse a still-running service - it will be re-enabled and restarted after the install, "
            "with its startup verified."
        )
        return

    old_uninstaller = existing.directory / "uninstall.sh"
    if old_uninstaller.exists() and os.access(old_uninstaller, os.X_OK):
        print(f"Uninstalling the existing installation via {old_uninstaller} ...")
        try:
            # It's baked to always run in uninstall mode regardless of argv, so
            # nothing needs to be passed - just answer its "remove the
            # directory?" prompt with yes, since we're about to reinstall there.
            subprocess.run([str(old_uninstaller)], input="y\n", text=True, check=True)
            return
        except subprocess.CalledProcessError as exc:
            warn(f"{old_uninstaller} exited with an error ({exc}); falling back to removing it directly.")

    warn(f"No usable uninstall.sh found at {existing.directory}; removing the existing installation directly.")
    stop_and_disable(mode, service_name)
    remove_unit(mode, service_name)
    if existing.directory.exists():
        shutil.rmtree(existing.directory)


def handle_directory_mismatch(
    existing: ExistingInstall, mode: str, target_dir: Path, install_systemd: bool, service_name: str,
) -> tuple[Path, bool, str]:
    version_note = f"v{existing.version}" if existing.version else "version unknown"
    print(f"An existing systemd installation found at {existing.directory} ({version_note}).")
    print(f"Current installation directory: {target_dir}.")
    choice = ask_choice(
        "What do you want to do?",
        {
            "s": "Skip installing the systemd service (leave the existing one as-is)",
            "n": "Install the systemd service under a different unit name",
            "x": "Cancel",
        },
    )
    if choice == "s":
        return target_dir, False, service_name
    if choice == "n":
        default_alt = f"{APP_NAME}-2.service"
        while True:
            entered = input(f"New systemd service name [{default_alt}]: ").strip() or default_alt
            alt_service_name = entered if entered.endswith(".service") else f"{entered}.service"
            if not unit_path_for(mode, alt_service_name).exists():
                return target_dir, install_systemd, alt_service_name
            print(f"{alt_service_name} already exists too; pick another name.")
    print("Installation cancelled.")
    sys.exit(1)


def handle_existing_install(
    existing: ExistingInstall, mode: str, target_dir: Path, install_systemd: bool, service_name: str, args: argparse.Namespace,
) -> tuple[Path, bool, str]:
    same_dir = existing.directory == target_dir

    if existing.version is None or CURRENT_VERSION is None:
        warn(f"Found an existing installation at {existing.directory}, but could not determine its version.")
        if not same_dir:
            return handle_directory_mismatch(existing, mode, target_dir, install_systemd, service_name)
        return resolve_conflict(target_dir), install_systemd, service_name

    cmp = compare_versions(CURRENT_VERSION, existing.version)
    if cmp is None:
        warn(
            f"Found an existing installation at {existing.directory} (version {existing.version!r}), "
            f"but couldn't compare it against the current version ({CURRENT_VERSION!r})."
        )
        if not same_dir:
            return handle_directory_mismatch(existing, mode, target_dir, install_systemd, service_name)
        return resolve_conflict(target_dir), install_systemd, service_name

    if not same_dir:
        # Per spec, this branch only applies when versions match; upgrades and
        # downgrades are treated as in-place operations on the same directory
        # the systemd unit already points at.
        if cmp == 0:
            return handle_directory_mismatch(existing, mode, target_dir, install_systemd, service_name)
        target_dir = existing.directory
        same_dir = True

    if cmp == 0:
        print("An existing installation found.\nReinstall?")
        proceed = ask_yes_no("Reinstall? [y/N] ", default=False)
    elif cmp > 0:
        print("Upgrade possible!")
        proceed = ask_yes_no(
            f"Upgrade from v{existing.version} to v{CURRENT_VERSION}? [Y/n] ", default=True
        )
    else:
        print("Newer version is already installed. Downgrade?")
        proceed = ask_yes_no(
            f"Downgrade from v{existing.version} to v{CURRENT_VERSION}? [y/N] ", default=False
        )

    if not proceed:
        print("Installation cancelled.")
        sys.exit(1)

    teardown_old_install(existing, mode, service_name, args.overwrite)
    return target_dir, install_systemd, service_name


def run_uninstall(args: argparse.Namespace) -> None:
    if _FORCE_UNINSTALL:
        mode = _BAKED_MODE
        install_dir = Path(_BAKED_INSTALL_DIR)
        print(f"Running bundled uninstaller for the {mode} install at {install_dir}.")
    else:
        mode = resolve_mode(args)
        install_dir = resolve_install_dir(mode, args)
        print(f"Uninstalling {mode} install at {install_dir}...")

    stop_and_disable(mode, SERVICE_NAME)
    remove_unit(mode, SERVICE_NAME)

    if install_dir.exists():
        if ask_yes_no(f"Remove install directory {install_dir}? [y/N] ", default=False):
            shutil.rmtree(install_dir)
            print(f"  Removed {install_dir}")
        else:
            print("  Left install directory in place.")
    else:
        print(f"  Install directory {install_dir} does not exist; nothing to remove there.")

    print("Uninstall complete.")


def main(argv=None) -> int:
    args = parse_args(argv)
    if _FORCE_UNINSTALL:
        args.uninstall = True

    system_hit, user_hit = check_already_installed()
    if system_hit:
        print(f"Note: found an existing system-wide install ({system_hit}).")
    if user_hit:
        print(f"Note: found an existing user-scope install ({user_hit}).")

    if args.uninstall:
        run_uninstall(args)
        return 0

    check_environment_or_prompt()

    mode = resolve_mode(args)
    target_dir = resolve_install_dir(mode, args)
    install_systemd = not args.no_systemd
    service_name = SERVICE_NAME

    existing = detect_existing_install(mode, target_dir)
    if existing is not None:
        target_dir, install_systemd, service_name = handle_existing_install(
            existing, mode, target_dir, install_systemd, service_name, args
        )
    else:
        target_dir = resolve_conflict(target_dir)

    perform_install(mode, target_dir, install_systemd=install_systemd, service_name=service_name)

    if install_systemd and not enable_and_start_service(mode, service_name):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
