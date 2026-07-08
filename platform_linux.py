"""Linux/X11 backend: python-xlib + ewmh for real per-pixel window shaping
(the X Shape extension), always-on-top/skip-taskbar (EWMH), and RandR
multi-monitor detection; pynput for the global quit hotkey.

Unlike Windows' color-key trick, the X Shape extension's ShapeBounding
region genuinely removes the window outside the sprite's silhouette - no
compositor needed, and mouse input follows the same shape automatically, so
clicks are pixel-precise just like on Windows.

Known limitations:
- The global Ctrl+Shift+Q hotkey uses the X11 RECORD extension (via pynput)
  and does not fire under native Wayland sessions (GNOME/KDE Wayland), even
  through XWayland - the tray icon's "Exit" item is the reliable quit path
  there. Window shaping/topmost/multi-monitor all keep working under
  XWayland since those ride the X11 protocol layer directly.
- EWMH's _NET_WORKAREA is one rect for the whole virtual desktop, not
  per-monitor, so the per-monitor work area below is a heuristic (see
  _get_monitors_via_randr).
"""

import os
import threading

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

# The X Shape extension removes non-sprite pixels from the window entirely,
# so it doesn't matter what color sits behind them - reuse the same
# flatten-onto-colorkey path as Windows rather than a third image pipeline.
NEEDS_COLOR_KEY_FLATTEN = True

_SHAPE_SET = 0  # Xlib.ext.shape.SO.Set
_SHAPE_BOUNDING = 0  # Xlib.ext.shape.SK.Bounding

_display = None


def _get_display():
    global _display
    if _display is None:
        from Xlib import display as xdisplay

        _display = xdisplay.Display()
    return _display


def set_dpi_awareness():
    pass  # X11 has no per-process DPI switch; Tk scales via Xft.dpi itself


def hide_from_taskbar_dock():
    pass  # handled per-window below via EWMH skip-taskbar/skip-pager


def get_data_dir(app_name):
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dir = os.path.join(base, app_name)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_all_monitors():
    """Work area of every connected monitor, sorted left to right. Falls back
    to the whole X screen (no work-area exclusion) if RandR monitor info or
    the SHAPE-adjacent EWMH work-area property isn't available."""
    monitors = None
    try:
        monitors = _get_monitors_via_randr()
    except Exception as exc:
        print(f"[Russgeist] Monitor detection failed ({exc}); using the whole screen instead.")

    if not monitors:
        screen = _get_display().screen()
        monitors = [{"work": (0, 0, screen.width_in_pixels, screen.height_in_pixels)}]

    monitors.sort(key=lambda m: m["work"][0])
    return monitors


def _get_monitors_via_randr():
    from ewmh import EWMH

    d = _get_display()
    root = d.screen().root
    reply = root.xrandr_get_monitors(is_active=True)
    rects = [
        (m.x, m.y, m.x + m.width_in_pixels, m.y + m.height_in_pixels)
        for m in reply.monitors
    ]
    if not rects:
        return None

    vd_left = min(r[0] for r in rects)
    vd_top = min(r[1] for r in rects)
    vd_right = max(r[2] for r in rects)
    vd_bottom = max(r[3] for r in rects)

    # _NET_WORKAREA is one rect for the whole virtual desktop (no per-monitor
    # concept in EWMH), so derive how far a panel eats into each edge by
    # diffing it against the full virtual-desktop bounds, then only apply
    # that inset to whichever monitor(s) actually touch that edge.
    inset_left = inset_top = inset_right = inset_bottom = 0
    work_areas = EWMH(_display=d).getWorkArea()
    if work_areas:
        wx, wy, ww, wh = work_areas[:4]
        inset_left = max(0, wx - vd_left)
        inset_top = max(0, wy - vd_top)
        inset_right = max(0, vd_right - (wx + ww))
        inset_bottom = max(0, vd_bottom - (wy + wh))

    monitors = []
    for left, top, right, bottom in rects:
        if left == vd_left:
            left += inset_left
        if top == vd_top:
            top += inset_top
        if right == vd_right:
            right -= inset_right
        if bottom == vd_bottom:
            bottom -= inset_bottom
        monitors.append({"work": (left, top, right, bottom)})
    return monitors


def configure_transparent_window(win, label, transparent_key):
    # No Tk-level attribute needed - the X Shape extension (set once winfo_id()
    # is available, see SpriteWindowBackend/on_frame_changed) does the work.
    # Any bg color is fine here since it's clipped away entirely.
    win.configure(bg=transparent_key)
    label.configure(bg=transparent_key)


def build_shape_mask(rgba_image):
    """RLE the alpha channel into horizontal (x, y, width, 1) rectangles for
    the X Shape extension's ShapeBounding region. Called once per unique
    pre-rendered frame (see main.py's _rotated_frame) and cached on that
    frame, not recomputed per tick."""
    w, h = rgba_image.size
    alpha = rgba_image.split()[-1].load()
    rects = []
    for y in range(h):
        x = 0
        while x < w:
            if alpha[x, y]:
                x0 = x
                while x < w and alpha[x, y]:
                    x += 1
                rects.append((x0, y, x - x0, 1))
            else:
                x += 1
    return rects


def _resolve_toplevel_xwindow(d, win_id):
    """Tk's winfo_id() returns an inner content window, not the actual
    top-level X window the WM/compositor manages (they're one level apart
    even under overrideredirect, which skips WM reparenting but not this
    internal Tk wrapper) - walk up the parent chain to the window that's a
    direct child of the root, which is what EWMH/Shape calls need to target."""
    root = d.screen().root
    xwin = d.create_resource_object("window", win_id)
    while True:
        parent = xwin.query_tree().parent
        if parent.id == root.id:
            return xwin
        xwin = parent


class SpriteWindowBackend:
    def __init__(self, win, transparent_key):
        from ewmh import EWMH

        d = _get_display()
        self._xwin = _resolve_toplevel_xwindow(d, win.winfo_id())
        self._ewmh = EWMH(_display=d)
        win.wm_attributes("-type", "utility")
        self._apply_above()

    def _apply_above(self):
        self._ewmh.setWmState(self._xwin, 1, "_NET_WM_STATE_ABOVE", "_NET_WM_STATE_SKIP_TASKBAR")
        self._ewmh.setWmState(self._xwin, 1, "_NET_WM_STATE_SKIP_PAGER")
        self._ewmh.display.flush()

    def reassert_topmost(self):
        self._apply_above()

    def on_frame_changed(self, shape_mask):
        if not shape_mask:
            return
        self._xwin.shape_rectangles(_SHAPE_SET, _SHAPE_BOUNDING, 0, 0, 0, shape_mask)
        self._ewmh.display.flush()


# ---------------------------------------------------------------------------
# Global quit hotkey (Ctrl+Shift+Q) via pynput's X11 RECORD-extension-based
# listener. Does not fire under native Wayland sessions - the tray icon's
# "Exit" item remains the reliable quit path there.
# ---------------------------------------------------------------------------
def register_quit_hotkey(callback):
    try:
        from pynput import keyboard
    except ImportError:
        print("[Russgeist] pynput not installed - Ctrl+Shift+Q disabled, use the tray icon to quit.")
        return None

    try:
        listener = keyboard.GlobalHotKeys({"<ctrl>+<shift>+q": callback})
        listener.daemon = True
        listener.start()
        return listener
    except Exception as exc:
        is_wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        hint = " (expected under native Wayland)" if is_wayland else ""
        print(f"[Russgeist] Could not register Ctrl+Shift+Q{hint}: {exc}. Use the tray icon to quit.")
        return None


def unregister_quit_hotkey(handle):
    if handle is not None:
        handle.stop()
