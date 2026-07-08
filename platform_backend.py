"""Picks the right OS-specific backend module and re-exports its interface
(see platform_windows.py, platform_macos.py, platform_linux.py for the
common contract each one implements)."""

import sys

if sys.platform == "win32":
    from platform_windows import *  # noqa: F401,F403
elif sys.platform == "darwin":
    from platform_macos import *  # noqa: F401,F403
else:
    from platform_linux import *  # noqa: F401,F403
