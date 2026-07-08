"""macOS backend: pyobjc (Cocoa/AppKit) for real per-pixel window
transparency, NSWindow-level topmost, Dock/Cmd-Tab hiding, per-screen work
areas, and a global quit hotkey via an NSEvent monitor.

Unlike Windows/Linux, this needs no color-key hack: Tk-Aqua supports true
alpha-channel transparency, so the sprite's own RGBA pixels are handed to Tk
directly (see main.py's NEEDS_COLOR_KEY_FLATTEN check).

Known limitation: Cocoa hit-tests a window's full rectangle, not its alpha
channel, so clicking a sprite's transparent corners still drags it (Windows'
color-key and Linux's X Shape extension are both pixel-precise). Accepted as
a minor platform difference rather than adding a custom NSView hit-test.
"""

import os

__all__ = [
    "NEEDS_COLOR_KEY_FLATTEN",
    "set_dpi_awareness",
    "hide_from_taskbar_dock",
    "get_data_dir",
    "get_all_monitors",
    "configure_transparent_window",
    "build_shape_mask",
    "SpriteWindowBackend",
    "register_quit_hotkey",
    "unregister_quit_hotkey",
]

NEEDS_COLOR_KEY_FLATTEN = False


def set_dpi_awareness():
    pass  # Tk-Aqua/Cocoa scale for Retina automatically - no per-process switch to flip


def hide_from_taskbar_dock():
    """Removes the Dock icon and Cmd-Tab entry at runtime - no .app bundle or
    Info.plist LSUIElement needed. Must be called after tk.Tk() has run, since
    that's what creates the shared NSApplication."""
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def get_data_dir(app_name):
    data_dir = os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_all_monitors():
    """Work area (menu bar + Dock excluded) of every connected screen, sorted
    left to right. NSScreen's coordinate space is bottom-left-origin relative
    to the primary screen; flip Y here so callers get the same top-left-origin
    (left, top, right, bottom) tuples as the Windows/Linux backends."""
    from AppKit import NSScreen

    screens = NSScreen.screens()
    if not screens:
        return [{"work": (0, 0, 1920, 1080)}]

    primary_height = screens[0].frame().size.height
    monitors = []
    for screen in screens:
        frame = screen.visibleFrame()
        left = frame.origin.x
        right = frame.origin.x + frame.size.width
        top = primary_height - (frame.origin.y + frame.size.height)
        bottom = primary_height - frame.origin.y
        monitors.append({"work": (int(left), int(top), int(right), int(bottom))})
    monitors.sort(key=lambda m: m["work"][0])
    return monitors


def configure_transparent_window(win, label, transparent_key):
    win.wm_attributes("-transparent", True)
    win.configure(bg="systemTransparent")
    label.configure(bg="systemTransparent")


def build_shape_mask(rgba_image):
    return None  # real alpha transparency needs no window-shape mask


class SpriteWindowBackend:
    """Finds this sprite's NSWindow (via a unique Tk window title set by
    main.py) and sets its window level directly, since Tk's own '-topmost'
    attribute doesn't reliably hold across Spaces/full-screen apps."""

    def __init__(self, win, transparent_key):
        self._ns_window = self._find_ns_window(win.title())
        self._apply_level()

    @staticmethod
    def _find_ns_window(title):
        from AppKit import NSApp

        for window in NSApp.windows():
            if window.title() == title:
                return window
        return None

    def _apply_level(self):
        if self._ns_window is None:
            return
        from AppKit import (
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorIgnoresCycle,
            NSWindowCollectionBehaviorStationary,
        )
        from Quartz import kCGStatusWindowLevel

        self._ns_window.setLevel_(kCGStatusWindowLevel)
        self._ns_window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )

    def reassert_topmost(self):
        self._apply_level()

    def on_frame_changed(self, shape_mask):
        pass  # real alpha transparency needs no per-frame window-shape update


# ---------------------------------------------------------------------------
# Global quit hotkey (Ctrl+Shift+Q) via a Cocoa global event monitor - needs
# the Accessibility permission (System Settings > Privacy & Security), which
# macOS prompts for automatically the first time this is registered.
# ---------------------------------------------------------------------------
def register_quit_hotkey(callback):
    from AppKit import (
        NSEvent,
        NSEventMaskKeyDown,
        NSEventModifierFlagControl,
        NSEventModifierFlagShift,
    )

    watched_flags = NSEventModifierFlagControl | NSEventModifierFlagShift

    def _handler(event):
        if (event.modifierFlags() & watched_flags) == watched_flags \
                and event.charactersIgnoringModifiers().lower() == "q":
            callback()

    try:
        return NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, _handler)
    except Exception as exc:
        print(f"[Russgeist] Could not register Ctrl+Shift+Q ({exc}); use the tray icon to quit instead.")
        return None


def unregister_quit_hotkey(handle):
    if handle is not None:
        from AppKit import NSEvent

        NSEvent.removeMonitor_(handle)
