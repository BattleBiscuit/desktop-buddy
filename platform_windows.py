"""Windows backend: ctypes calls into user32.dll/shcore.dll for window
transparency (layered + color-key), click-through, per-monitor work areas,
topmost, hiding from the taskbar/Alt-Tab, and the global quit hotkey.
"""

import ctypes
import os
import threading
from ctypes import wintypes

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

# Color-key transparency needs the sprite flattened onto an opaque
# background first - see main.py's _rotated_frame/_composite_on_key.
NEEDS_COLOR_KEY_FLATTEN = True


# ---------------------------------------------------------------------------
# DPI awareness - must be set before any window is created, or the sprite
# will be upscaled/blurred by Windows' bitmap DPI virtualization.
# ---------------------------------------------------------------------------
def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def hide_from_taskbar_dock():
    pass  # handled per-window below via WS_EX_TOOLWINDOW


def get_data_dir(app_name):
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    data_dir = os.path.join(base, app_name)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# ---------------------------------------------------------------------------
# Win32 constants and API bindings
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

LWA_COLORKEY = 0x00000001

SPI_GETWORKAREA = 0x0030

SM_CXSCREEN = 0
SM_CYSCREEN = 1

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_Q = 0x51
HOTKEY_ID = 1

try:
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _get_ex_style = user32.GetWindowLongPtrW
    _set_ex_style = user32.SetWindowLongPtrW
except AttributeError:
    # 32-bit fallback for very old Windows/Python builds.
    _get_ex_style = user32.GetWindowLongW
    _set_ex_style = user32.SetWindowLongW

user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _get_work_area():
    """Primary monitor's usable desktop area (taskbar excluded). Used as a fallback
    if per-monitor enumeration below is unavailable."""
    rect = RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    w = user32.GetSystemMetrics(SM_CXSCREEN)
    h = user32.GetSystemMetrics(SM_CYSCREEN)
    return 0, 0, w, h


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), _MonitorEnumProc, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]


def get_all_monitors():
    """Work area (taskbar excluded) of every connected monitor, sorted left to right."""
    monitors = []

    def _callback(hmonitor, hdc, lprect, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitors.append({"work": (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)})
        return 1  # continue enumeration

    if not user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(_callback), 0) or not monitors:
        monitors = [{"work": _get_work_area()}]
    monitors.sort(key=lambda m: m["work"][0])
    return monitors


def configure_transparent_window(win, label, transparent_key):
    win.attributes("-transparentcolor", transparent_key)
    win.configure(bg=transparent_key)
    label.configure(bg=transparent_key)


def build_shape_mask(rgba_image):
    return None  # color-key transparency needs no window-shape mask


class SpriteWindowBackend:
    """Layered (for color-key transparency) + hidden from taskbar/Alt-Tab +
    always-on-top. Deliberately NOT click-through (WS_EX_TRANSPARENT): the
    sprites need to receive mouse clicks so they can be dragged.

    Rewriting GWL_EXSTYLE via SetWindowLongPtrW resets the layered window's
    color-key, which Windows then paints as solid black instead of
    transparent. Re-asserting SetLayeredWindowAttributes afterwards (on top
    of Tk's own "-transparentcolor" call) keeps the color-key intact.
    """

    def __init__(self, win, transparent_key):
        from PIL import ImageColor

        self._hwnd = win.winfo_id()
        self._apply_styles(ImageColor.getrgb(transparent_key))

    def _apply_styles(self, transparent_key_rgb):
        hwnd = self._hwnd
        ex_style = _get_ex_style(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        ex_style &= ~WS_EX_APPWINDOW
        _set_ex_style(hwnd, GWL_EXSTYLE, ex_style)

        r, g, b = transparent_key_rgb
        colorref = r | (g << 8) | (b << 16)
        user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY)

        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def reassert_topmost(self):
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def on_frame_changed(self, shape_mask):
        pass  # color-key transparency needs no per-frame window-shape update


# ---------------------------------------------------------------------------
# Global quit hotkey (Ctrl+Shift+Q), delivered even when no window has focus.
# ---------------------------------------------------------------------------
def register_quit_hotkey(callback):
    handle = {"thread_id": None}

    def _listen():
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_Q):
            print("[Russgeist] Could not register Ctrl+Shift+Q (already in use by another app).")
            return
        handle["thread_id"] = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    callback()
                    break
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    thread = threading.Thread(target=_listen, daemon=True)
    thread.start()
    return handle


def unregister_quit_hotkey(handle):
    if handle and handle.get("thread_id"):
        user32.PostThreadMessageW(handle["thread_id"], WM_QUIT, 0, 0)
