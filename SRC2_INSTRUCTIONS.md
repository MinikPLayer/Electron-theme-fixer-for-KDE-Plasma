# Task Instructions (src/ rewrite)

In fixer.py you have a working fixer implementation of the electron apps theme sync fix.
In fixer.py, watcher_test.py and watcher_gio.py you have PoC of different theme change event detection mechanisms.
Use them to create the final electron-theme-fixer.py script, with following instructions:

- Base the final solution on the fixer.py structure. This is your starting point.
- Extend the fixer.py with an ThemeEventDetectionStrategy interface. Implementations of this interface will be
  responsible for detecting when to fire the ThemeFixStrategy implementation.
- Create 3 implementations of the Event Detection Strategy: DBusEventDetectionStrategy (present in the fixer.py
  file), GSettingsEventDetectionStrategy (from watcher_gio.py) and GtkFileWatcherDetectionStrategy (from
  watcher_test.py).
- Each detection strategy MUST be in a separate file and imported ONLY if necessary. This could allow for detection
  strategies, which uses libraries not installed in the system (which means you CAN'T import these files unless
  they are selected to be used).
- Create the final implementation in a `src` directory. DON'T delete or modify original files - write new ones.

## Testing notes

- You are allowed to test the solution. You can use the `plasma-apply-colorscheme` to trigger a theme change. You
  are NOT allowed to permanently alter any configs (but you ARE allowed to change some files, and then revert
  these changes).
