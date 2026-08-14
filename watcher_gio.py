import subprocess
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

import shutil

gdbus = shutil.which("gdbus")

NOTIFY_OBJECT_PATH = "/ca/desrt/dconf/Writer/user"
NOTIFY_SIGNAL = "ca.desrt.dconf.Writer.Notify"
NOTIFY_KEY_PATH = "/org/gnome/desktop/interface/"


last_call = 0
def on_color_scheme_changed(settings, key):
    global last_call

    value = settings.get_string(key)
    print(f"{key} changed: {value}")

    now = time.time()
    if now - last_call < 2:
        print("Ignoring rapid change")
        return
    
    time.sleep(0.4)
    subprocess.run(
        [
            gdbus, "emit", "--session",
            "--object-path", NOTIFY_OBJECT_PATH,
            "--signal", NOTIFY_SIGNAL,
            NOTIFY_KEY_PATH, "['']", ""
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    last_call = time.time()

settings = Gio.Settings.new("org.gnome.desktop.interface")

# The "changed::<key>" signal is your filtering mechanism —
# this only fires for color-scheme, not every key in the schema.
settings.connect("changed::icon-theme", on_color_scheme_changed)

# You need a running GLib main loop for signals to actually fire
loop = GLib.MainLoop()
loop.run()