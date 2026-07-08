"""
Russgeist - a handful of small soot-sprite (Susuwatari-style) desktop
companions that huddle and scurry along the Windows taskbar, roaming
across every connected monitor.

Windows-only. Run with: python main.py
"""

import ctypes
import json
import math
import os
import random
import sys
import threading
import time

if sys.platform != "win32":
    sys.exit("Russgeist only runs on Windows (needs user32.dll / shcore.dll via ctypes).")

from ctypes import wintypes

import tkinter as tk

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageTk
except ImportError:
    sys.exit("Pillow is required. Install with: pip install Pillow")

try:
    import pystray
    HAVE_PYSTRAY = True
except ImportError:
    HAVE_PYSTRAY = False


# ---------------------------------------------------------------------------
# DPI awareness - must be set before any window is created, or the sprite
# will be upscaled/blurred by Windows' bitmap DPI virtualization.
# ---------------------------------------------------------------------------
def _set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


_set_dpi_awareness()


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


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def get_work_area():
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
        monitors = [{"work": get_work_area()}]
    monitors.sort(key=lambda m: m["work"][0])
    return monitors


def monitor_for_x(x_center, monitors):
    """Which monitor a given x-coordinate currently sits over (nearest one if it's
    in a horizontal gap between two non-contiguous monitors)."""
    for m in monitors:
        left, _, right, _ = m["work"]
        if left <= x_center < right:
            return m

    def _distance(m):
        left, _, right, _ = m["work"]
        return (left - x_center) if x_center < left else (x_center - right + 1)

    return min(monitors, key=_distance)


def monitor_for_point(x, y, monitors):
    """Which monitor a given (x, y) point currently sits over (nearest one if
    it's outside every monitor's work area)."""
    for m in monitors:
        left, top, right, bottom = m["work"]
        if left <= x < right and top <= y < bottom:
            return m

    def _distance(m):
        left, top, right, bottom = m["work"]
        dx = 0 if left <= x < right else min(abs(x - left), abs(x - right))
        dy = 0 if top <= y < bottom else min(abs(y - top), abs(y - bottom))
        return dx + dy

    return min(monitors, key=_distance)


def apply_window_styles(hwnd, transparent_key_rgb):
    """Layered (for color-key transparency) + hidden from taskbar/Alt-Tab +
    always-on-top. Deliberately NOT click-through (WS_EX_TRANSPARENT): the
    sprites need to receive mouse clicks so they can be dragged.

    Rewriting GWL_EXSTYLE via SetWindowLongPtrW resets the layered window's
    color-key, which Windows then paints as solid black instead of
    transparent. Re-asserting SetLayeredWindowAttributes afterwards (on top
    of Tk's own "-transparentcolor" call) keeps the color-key intact.
    """
    ex_style = _get_ex_style(hwnd, GWL_EXSTYLE)
    ex_style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    ex_style &= ~WS_EX_APPWINDOW
    _set_ex_style(hwnd, GWL_EXSTYLE, ex_style)

    r, g, b = transparent_key_rgb
    colorref = r | (g << 8) | (b << 16)
    user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY)

    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def reassert_topmost(hwnd):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# ---------------------------------------------------------------------------
# Soot sprite art - a single procedurally-drawn pose per instance. Motion is
# conveyed by rotating this one drawing (a waddle while walking, an
# occasional shake while idle) rather than switching between different
# drawn poses.
# ---------------------------------------------------------------------------
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_data_dir():
    """Where per-user data (settings, and the generated sprite asset) lives.
    Next to the script when run from source; a per-user AppData folder when
    packaged as a frozen exe (e.g. via PyInstaller --onefile), since a
    onefile build's own directory is a temp extraction folder that gets
    wiped on exit - writing there would silently reset settings every run."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(base, "Russgeist")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return ASSET_DIR


DATA_DIR = _get_data_dir()
TRANSPARENT_KEY = "#ff00fe"  # color-keyed as fully transparent by the OS
SPRITE_FILE = "sootsprite.png"

SOOT_COLOR = (22, 17, 13, 255)
EYE_COLOR = (253, 250, 242, 255)
# A hair lighter than SOOT_COLOR - just enough rim to separate the sprite from a
# near-black taskbar/background without reading as a drawn-on cartoon outline.
OUTLINE_COLOR = (72, 62, 52, 255)

SPRITE_SIZE_RANGE = (22, 60)  # each spawned sprite gets its own random size; 60 is the ceiling
SPRITE_COUNT = 5

WALK_WADDLE_DEG = 14
WALK_WADDLE_STEPS = 12
SHAKE_SEQUENCE_DEG = [0, -9, 7, -6, 4, -2, 0]


def draw_soot_sprite(size):
    """A fuzzy, spiky-furred soot sprite (Russgeist / Susuwatari-style)."""
    w = h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = w / 2, h * 0.50
    r = w * 0.33

    # spiky fur silhouette: a jagged ring of irregular tufts, then a plain
    # circle on top to round out the body core, leaving the spike tips
    # poking out unevenly - regular alternating spikes read as a gear/star,
    # randomized length (and valley depth) reads as fluffy, chaotic fur.
    n = 26
    pts = []
    for i in range(n * 2):
        ang = (i / (n * 2)) * 2 * math.pi
        rad = r * (random.uniform(1.15, 1.42) if i % 2 else random.uniform(0.9, 1.02))
        pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
    draw.polygon(pts, fill=SOOT_COLOR)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SOOT_COLOR)

    # eyes
    er = r * 0.33
    ex = r * 0.40
    ey = cy - r * 0.10
    for s in (-1, 1):
        exx = cx + s * ex
        draw.ellipse([exx - er, ey - er, exx + er, ey + er], fill=EYE_COLOR)
        pr = er * 0.45
        pcy = ey + er * 0.12
        draw.ellipse([exx - pr, pcy - pr, exx + pr, pcy + pr], fill=SOOT_COLOR)
        hr = max(er * 0.16, 1)
        hx, hy = exx - pr * 0.5, pcy - pr * 0.5
        draw.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=EYE_COLOR)

    return img


def _eye_geometry(size):
    """Same eye placement math as the eyes block in draw_soot_sprite, re-derived
    from an already-rendered image's size so wink variants (below) line up on
    both the procedurally-drawn default and on hand-drawn art following the
    same face layout."""
    r = size * 0.33
    cx, cy = size / 2, size * 0.50
    er = r * 0.33
    ex = r * 0.40
    ey = cy - r * 0.10
    return cx, ex, ey, er


def make_wink_variant(base_rgba, wink):
    """A copy of `base_rgba` with one or both eyes shut. `wink` is "left",
    "right" (a one-eyed wink) or "both" (a two-eyed blink). The open eye (white
    + pupil + highlight) is painted over with the body color, then a single
    curved eyelid line is drawn in its place - cheaper than keeping a whole
    second hand-drawn pose around for what's a small, localized change."""
    size = base_rgba.size[0]
    cx, ex, ey, er = _eye_geometry(size)
    img = base_rgba.copy()
    draw = ImageDraw.Draw(img)
    pad = er * 1.15
    lw = max(int(er * 0.35), 2)
    for s in (-1, 1):
        if wink != "both" and wink != ("left" if s == -1 else "right"):
            continue
        exx = cx + s * ex
        draw.ellipse([exx - pad, ey - pad, exx + pad, ey + pad], fill=SOOT_COLOR)
        draw.arc([exx - er, ey - er, exx + er, ey + er], start=200, end=340, fill=OUTLINE_COLOR, width=lw)
    return img


def ensure_asset():
    path = os.path.join(DATA_DIR, SPRITE_FILE)
    if not os.path.isfile(path):
        print(f"[Russgeist] '{SPRITE_FILE}' not found, generating a placeholder sprite.")
        draw_soot_sprite(160).save(path)


# ---------------------------------------------------------------------------
# Persisted app settings - every context-menu switch plus the current sprite
# count, so relaunching the app comes back the way it was left instead of
# resetting to the hardcoded defaults every time.
# ---------------------------------------------------------------------------
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "jumping_enabled": True,
    "caged": False,
    "paused": False,
    "sprite_count": SPRITE_COUNT,
}


def load_settings():
    path = os.path.join(DATA_DIR, SETTINGS_FILE)
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            settings.update({k: loaded[k] for k in DEFAULT_SETTINGS if k in loaded})
    except (OSError, ValueError):
        pass  # missing/corrupt settings file - fall back to defaults
    return settings


def save_settings(settings):
    path = os.path.join(DATA_DIR, SETTINGS_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


# The base drawing's legs point straight down. Rotating by these amounts
# (PIL rotates counter-clockwise for positive degrees) makes the legs point
# at whichever edge the sprite is currently clinging to.
EDGES = ("bottom", "right", "top", "left")
EDGE_BASE_ANGLE = {"bottom": 0, "right": 90, "top": 180, "left": 270}


def _composite_on_key(rgba_image):
    """Flatten an RGBA sprite onto the transparent-key color for color-key transparency."""
    key_rgb = ImageColor.getrgb(TRANSPARENT_KEY)
    bg = Image.new("RGBA", rgba_image.size, key_rgb + (255,))
    bg.paste(rgba_image, (0, 0), rgba_image)
    return bg.convert("RGB")


def _rotated_frame(base_rgba, angle_deg):
    """Rotate with NEAREST resampling (keeps alpha binary, avoiding a
    colored fringe where the color-key trick can't blend a soft edge)."""
    rotated = base_rgba.rotate(angle_deg, resample=Image.NEAREST, expand=False, fillcolor=(0, 0, 0, 0))
    return ImageTk.PhotoImage(_composite_on_key(rotated))


def _add_outline(rgba_image, color):
    """A 1px rim around the sprite's silhouette (from its alpha channel, so it works
    on any sprite art, not just the procedurally-drawn default). Applied post-resize,
    at each instance's actual on-screen size, so it stays a crisp single pixel instead
    of surviving a NEAREST downscale from the shared full-size asset as broken flecks."""
    dilated = rgba_image.split()[-1].filter(ImageFilter.MaxFilter(3))
    outlined = Image.new("RGBA", rgba_image.size, (0, 0, 0, 0))
    outlined.paste(color, (0, 0), dilated)
    outlined.paste(rgba_image, (0, 0), rgba_image)
    return outlined


def load_base_sprite(size):
    """Load the on-disk sprite (or the placeholder), sized for one instance."""
    path = os.path.join(DATA_DIR, SPRITE_FILE)
    with Image.open(path) as raw:
        base = raw.convert("RGBA").resize((size, size), Image.NEAREST)
    return _add_outline(base, OUTLINE_COLOR)


def build_sprite_frames(base_rgba, wink_variants, base_angle=0):
    """Pre-render every rotation frame for one edge orientation, so the
    animation loop only ever swaps a reference instead of drawing per-frame."""
    walk_angles = [
        base_angle + WALK_WADDLE_DEG * math.sin(2 * math.pi * i / WALK_WADDLE_STEPS)
        for i in range(WALK_WADDLE_STEPS)
    ]
    walk_frames = [_rotated_frame(base_rgba, a) for a in walk_angles]
    idle_frame = _rotated_frame(base_rgba, base_angle)
    shake_frames = [_rotated_frame(base_rgba, base_angle + a) for a in SHAKE_SEQUENCE_DEG]
    wink_frames = {name: _rotated_frame(variant, base_angle) for name, variant in wink_variants.items()}
    return walk_frames, idle_frame, shake_frames, wink_frames


# ---------------------------------------------------------------------------
# Soot sprite state machine - one instance per on-screen creature, each with
# its own draggable Toplevel window. Sprites cling to whichever screen edge
# (bottom/top/left/right) they're currently on and wobble along it; dropping
# one after a drag sends it scurrying to the nearest edge.
# ---------------------------------------------------------------------------
class SootSprite:
    IDLE = "idle"
    WALKING = "walking"
    HOPPING = "hopping"
    JUMPING = "jumping"
    RETURNING = "returning"
    THROWN = "thrown"
    BODYCHECK = "bodycheck"

    FRAME_MS = 50
    WALK_SPEED = 2    # px per tick while walking along an edge
    RETURN_SPEED = 5  # px per tick while scurrying back to an edge after a drag
    MIN_IDLE_MS = 2000
    MAX_IDLE_MS = 6000
    MIN_WALK_MS = 1200
    MAX_WALK_MS = 3500
    MIN_SHAKE_GAP_MS = 2500
    MAX_SHAKE_GAP_MS = 7000
    REASSERT_MS = 2000

    # -- winking (an idle-only eye animation, same "queue of pre-rendered frames"
    # approach as shaking above): most of the time it's an ordinary two-eyed
    # blink, held just long enough to register; occasionally a single-eyed wink,
    # held noticeably longer so it reads as a deliberate wink and not a glitch --
    MIN_WINK_GAP_MS = 3000
    MAX_WINK_GAP_MS = 9000
    BLINK_HOLD_TICKS = 2
    WINK_HOLD_TICKS = 10
    WINK_ONE_EYE_PROBABILITY = 0.3

    # -- hopping (a bouncy walk that stays on the current edge) --
    HOP_SPEED = 3          # px per tick along the edge while hopping
    HOP_HEIGHT = 6         # px, how far each bob lifts away from the edge
    HOP_CYCLE_TICKS = 10   # ticks per bob (up-and-down)

    # -- jumping (a real leap; this is how a sprite can switch edges on its own -
    # jumping off anything but the floor gives gravity a chance to pull it down to
    # the bottom edge instead of snapping back to the wall it jumped from) --
    JUMP_INWARD_SPEED = 9   # px/tick launch speed, away from whichever edge it's on
    JUMP_ALONG_SPEED = 2.5  # px/tick drift along the edge, in the current direction

    # -- bodycheck (a rare bump when two idle sprites on the same edge end up
    # close together): the initiator recoils in place, the sprite it hits gets
    # launched with the same airborne physics as a drag-throw, just gentler --
    BODYCHECK_PROBABILITY = 0.50  # rolled only when a neighbor is already in range - the 55px range keeps it rare, not this
    BODYCHECK_RANGE = 55          # px apart (along the edge) to count as "near enough" to bump
    BODYCHECK_SPEED = 7           # px/tick the initiator recoils backward
    BODYCHECK_TICKS = 8           # how long the initiator's recoil lasts
    BODYCHECK_HIT_ALONG_SPEED = 10   # px/tick launch speed along the edge for the sprite that gets hit
    BODYCHECK_HIT_INWARD_SPEED = 6   # px/tick launch speed away from the edge for the sprite that gets hit

    # -- throw/jump physics --
    VELOCITY_WINDOW_S = 0.1  # how much recent drag history to use for the flick velocity
    # FRAME_MS/1000 converts real mouse px/sec into sprite px/tick (our simulation step);
    # this is that 1:1 conversion, with MAX_THROW_SPEED doing the actual feel-tuning below.
    THROW_VELOCITY_SCALE = FRAME_MS / 1000
    MAX_THROW_SPEED = 45      # px/tick cap per axis, so a wild flick doesn't launch it off to infinity
    THROW_MIN_SPEED = 6       # below this, a release is a gentle drop, not a throw
    THROW_SETTLE_SPEED = 1.5  # once post-bounce (or post-jump) speed drops below this, airborne motion is done
    GRAVITY = 0.6             # px/tick^2 downward acceleration while airborne
    # Hard, rigid screen edges: bounces keep most of their speed (a springy wall,
    # not a soft/absorbent one). Only the OUTER bounds of the combined virtual
    # desktop bounce - the seam between two monitors is invisible to this physics,
    # so a hard enough throw sails straight across from one monitor to another.
    BOUNCE_DAMPING = 0.9
    # Continuous per-tick decay, independent of wall bounces. Without this, horizontal
    # speed only shrinks when it happens to hit a side wall - and each crossing takes
    # *longer* than the last as speed drops, so on a wide multi-monitor span it could
    # bounce for minutes before settling. This is what actually brings a hard-bouncing
    # sprite to rest, since BOUNCE_DAMPING alone barely dissipates any energy now.
    AIR_FRICTION = 0.98
    TUMBLE_STEPS = 16         # frames in the full-rotation tumble cycle while airborne

    def __init__(self, master, size, monitors, on_close=None, settings=None, sprites=None):
        self.win = tk.Toplevel(master)
        self.w = self.h = size
        self.base_rgba = load_base_sprite(size)
        self._wink_variants = {
            "left": make_wink_variant(self.base_rgba, "left"),
            "right": make_wink_variant(self.base_rgba, "right"),
            "both": make_wink_variant(self.base_rgba, "both"),
        }
        self._frame_cache = {}
        self.on_close = on_close
        self.settings = settings if settings is not None else {}
        # Shared with every other sprite (the same list `main()` appends new
        # spawns to) so bodychecks can find a neighbor to bump into.
        self.sprites = sprites if sprites is not None else []

        self.state = self.IDLE
        self.dragging = False
        self.paused = False
        self.closed = False
        self.direction = random.choice([-1, 1])
        self._shake_queue = []
        self._wink_queue = []

        self.monitors = monitors
        self._caged_monitor = random.choice(self.monitors)  # only used once caging is turned on
        self.edge = random.choice(EDGES)
        self._set_edge(self.edge)
        lo, hi = self._edge_range()
        varying = float(random.randint(lo, max(lo, hi)))
        if self.edge in ("bottom", "top"):
            self.x, self.y = varying, 0.0
        else:
            self.x, self.y = 0.0, varying
        self._snap_fixed_coord()
        self._walk_idx = random.randrange(len(self.walk_frames))

        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.win.configure(bg=TRANSPARENT_KEY)
        self.win.geometry(f"{self.w}x{self.h}+{int(self.x)}+{int(self.y)}")

        self.label = tk.Label(self.win, bd=0, highlightthickness=0, bg=TRANSPARENT_KEY)
        self.label.place(x=0, y=0, width=self.w, height=self.h)
        self._set_image(self.idle_frame)

        self.label.bind("<ButtonPress-1>", self._on_press)
        self.label.bind("<B1-Motion>", self._on_drag)
        self.label.bind("<ButtonRelease-1>", self._on_release)
        self.label.bind("<Button-3>", self._on_right_click)

        self.win.update_idletasks()
        self.hwnd = self.win.winfo_id()
        apply_window_styles(self.hwnd, ImageColor.getrgb(TRANSPARENT_KEY))

        self._schedule_next_state()
        self._schedule_next_shake()
        self._schedule_next_wink()
        self.win.after(random.randint(0, self.FRAME_MS), self.tick)
        self.win.after(self.REASSERT_MS, self._on_reassert_timer)

    # -- frames -----------------------------------------------------------

    def _set_edge(self, edge):
        self.edge = edge
        if edge not in self._frame_cache:
            self._frame_cache[edge] = build_sprite_frames(self.base_rgba, self._wink_variants, EDGE_BASE_ANGLE[edge])
        self.walk_frames, self.idle_frame, self.shake_frames, self.wink_frames = self._frame_cache[edge]

    def _set_image(self, img):
        self.label.configure(image=img)
        self.label.image = img

    # -- state timing -------------------------------------------------------

    def _schedule_next_state(self):
        if self.state == self.IDLE:
            duration = random.randint(self.MIN_IDLE_MS, self.MAX_IDLE_MS)
        else:
            duration = random.randint(self.MIN_WALK_MS, self.MAX_WALK_MS)
        self._state_ticks_left = max(1, duration // self.FRAME_MS)

    def _schedule_next_shake(self):
        gap = random.randint(self.MIN_SHAKE_GAP_MS, self.MAX_SHAKE_GAP_MS)
        self._shake_ticks_left = gap // self.FRAME_MS

    def _schedule_next_wink(self):
        gap = random.randint(self.MIN_WINK_GAP_MS, self.MAX_WINK_GAP_MS)
        self._wink_ticks_left = gap // self.FRAME_MS

    def _begin_wink(self):
        if random.random() < self.WINK_ONE_EYE_PROBABILITY:
            which, hold = random.choice(("left", "right")), self.WINK_HOLD_TICKS
        else:
            which, hold = "both", self.BLINK_HOLD_TICKS
        self._wink_queue = [self.wink_frames[which]] * hold

    def _decide_next_state(self):
        if self.state == self.IDLE:
            neighbor = self._find_bodycheck_neighbor()
            if neighbor is not None and random.random() < self.BODYCHECK_PROBABILITY:
                self._begin_bodycheck(neighbor)
                return  # bodycheck ends itself via its own tick countdown
            roll = random.random()
            if roll < 0.35:
                self.state = self.WALKING
                self.direction = random.choice([-1, 1])
            elif roll < 0.50:
                self.state = self.HOPPING
                self.direction = random.choice([-1, 1])
                self._hop_phase = 0
            elif roll < 0.60 and self.settings.get("jumping_enabled", True):
                # A jump off the floor always falls back to the floor (see _on_jump_settle) -
                # without this, a climb up a wall/ceiling is the only way back the other
                # direction, and every sprite would eventually ratchet down to the bottom
                # edge and stay there for good.
                if self.edge == "bottom" and random.random() < 0.35:
                    self._begin_climb()
                else:
                    self._begin_jump()
                return  # jump/climb end themselves, not via a tick countdown
            else:
                self.state = self.IDLE
        else:
            self.state = self.IDLE
        self._schedule_next_state()

    # -- edge geometry ------------------------------------------------------

    def _inward_delta(self, magnitude):
        """(dx, dy) that moves `magnitude` px away from the current edge, into the room."""
        if self.edge == "bottom":
            return 0, -magnitude
        if self.edge == "top":
            return 0, magnitude
        if self.edge == "left":
            return magnitude, 0
        return -magnitude, 0  # right

    def _along_pos(self):
        """The coordinate that varies while walking the current edge (x for
        top/bottom, y for left/right) - what "distance apart" means for two
        sprites clinging to the same edge."""
        return self.x if self.edge in ("bottom", "top") else self.y

    def _find_bodycheck_neighbor(self):
        """Another idle sprite on the same edge, close enough to bump into right now."""
        for other in self.sprites:
            if other is self or other.closed or other.dragging or other.paused:
                continue
            if other.edge != self.edge or other.state != self.IDLE:
                continue
            if abs(other._along_pos() - self._along_pos()) <= self.BODYCHECK_RANGE:
                return other
        return None

    def _begin_bodycheck(self, other):
        """The initiator recoils backward in place; the sprite it hits gets
        catapulted away with the same airborne physics as a drag-throw, just
        with less force behind it."""
        push_dir = 1 if other._along_pos() >= self._along_pos() else -1
        self._shove(-push_dir)
        other._begin_throw(*other._bodycheck_hit_velocity(push_dir))

    def _shove(self, push_dir):
        self.direction = push_dir
        self._bodycheck_ticks_left = self.BODYCHECK_TICKS
        self.state = self.BODYCHECK

    def _bodycheck_hit_velocity(self, push_dir):
        """Launch velocity for the sprite on the receiving end of a bodycheck: a
        knock in `push_dir` along the edge, plus a small pop away from the edge -
        the same shape as a self-initiated jump (see `_jump_velocity`), just
        gentler than the full force of an actual throw."""
        along = push_dir * self.BODYCHECK_HIT_ALONG_SPEED
        dx, dy = self._inward_delta(self.BODYCHECK_HIT_INWARD_SPEED)
        if self.edge in ("bottom", "top"):
            return along, dy
        return dx, along

    def _advance_along_edge(self, speed):
        """Move along the current edge by `speed` px, bouncing back at either end."""
        lo, hi = self._edge_range()
        pos = self.x if self.edge in ("bottom", "top") else self.y
        pos += self.direction * speed
        if pos <= lo:
            pos, self.direction = lo, 1
        elif pos >= hi:
            pos, self.direction = hi, -1
        if self.edge in ("bottom", "top"):
            self.x = pos
        else:
            self.y = pos
        self._snap_fixed_coord()

    def _is_caged(self):
        return self.settings.get("caged", False)

    def lock_to_current_monitor(self):
        """Pin this sprite to whichever monitor it's on right now - used when caging
        turns on, and again on every drop while caging is active, so dragging a
        caged sprite onto a different monitor re-homes it there."""
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        self._caged_monitor = monitor_for_point(cx, cy, self.monitors)

    def _current_monitor_rect(self):
        """Work-area rect this sprite is confined to right now: the locked monitor
        while caged, otherwise whichever one it's physically over."""
        if self._is_caged():
            return self._caged_monitor["work"]
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        return monitor_for_point(cx, cy, self.monitors)["work"]

    def _edge_range(self):
        """Valid (min, max) for the coordinate that varies while walking this edge."""
        if self._is_caged():
            left, top, right, bottom = self._caged_monitor["work"]
            if self.edge in ("bottom", "top"):
                return left, right - self.w
            return top, bottom - self.h
        if self.edge in ("bottom", "top"):
            overall_left = min(m["work"][0] for m in self.monitors)
            overall_right = max(m["work"][2] for m in self.monitors)
            return overall_left, overall_right - self.w
        m = (min if self.edge == "left" else max)(
            self.monitors, key=lambda mm: mm["work"][0 if self.edge == "left" else 2]
        )
        return m["work"][1], m["work"][3] - self.h

    def _virtual_bounds(self):
        """Bounding box a thrown sprite bounces off - every monitor's work area
        combined, or just the locked monitor while caged."""
        if self._is_caged():
            return self._caged_monitor["work"]
        left = min(m["work"][0] for m in self.monitors)
        top = min(m["work"][1] for m in self.monitors)
        right = max(m["work"][2] for m in self.monitors)
        bottom = max(m["work"][3] for m in self.monitors)
        return left, top, right, bottom

    def _snap_fixed_coord(self):
        """Pin the coordinate that does NOT vary to whichever edge we're on."""
        if self._is_caged():
            left, top, right, bottom = self._caged_monitor["work"]
        elif self.edge in ("bottom", "top"):
            left, top, right, bottom = monitor_for_x(self.x + self.w // 2, self.monitors)["work"]
        else:
            m = (min if self.edge == "left" else max)(
                self.monitors, key=lambda mm: mm["work"][0 if self.edge == "left" else 2]
            )
            left, top, right, bottom = m["work"]
        if self.edge == "bottom":
            self.y = bottom - self.h
        elif self.edge == "top":
            self.y = top
        elif self.edge == "left":
            self.x = left
        elif self.edge == "right":
            self.x = right - self.w

    def _nearest_edge(self):
        """Which edge of whichever monitor we're currently over (or caged to) is closest."""
        left, top, right, bottom = self._current_monitor_rect()
        candidates = {
            "top": abs(self.y - top),
            "bottom": abs((self.y + self.h) - bottom),
            "left": abs(self.x - left),
            "right": abs((self.x + self.w) - right),
        }
        return min(candidates, key=candidates.get), (left, top, right, bottom)

    def _begin_return_to_edge(self, forced_edge=None):
        """Settle onto an edge. With no argument, whichever edge is geometrically
        nearest (used after a drag-throw). `forced_edge` overrides that - used when
        a jump off a non-bottom edge must fall to the floor rather than snap back
        to the wall it left."""
        if forced_edge is not None:
            edge = forced_edge
            left, top, right, bottom = self._current_monitor_rect()
        else:
            edge, (left, top, right, bottom) = self._nearest_edge()
        self._set_edge(edge)
        if edge in ("top", "bottom"):
            target_x = min(max(self.x, left), right - self.w)
            target_y = top if edge == "top" else bottom - self.h
        else:
            target_y = min(max(self.y, top), bottom - self.h)
            target_x = left if edge == "left" else right - self.w
        self._return_target = (target_x, target_y)
        self._walk_idx = 0
        self.state = self.RETURNING

    # -- throwing ---------------------------------------------------------

    def _tumble_frames(self):
        """Full-rotation spin cycle used while airborne, cached like the edge frame sets."""
        if "tumble" not in self._frame_cache:
            steps = self.TUMBLE_STEPS
            angles = [i * 360 / steps for i in range(steps)]
            self._frame_cache["tumble"] = [_rotated_frame(self.base_rgba, a) for a in angles]
        return self._frame_cache["tumble"]

    def _begin_throw(self, vx, vy):
        self.vx, self.vy = vx, vy
        self._tumble = self._tumble_frames()
        self._tumble_idx = 0
        self.state = self.THROWN

    # -- jumping ------------------------------------------------------------

    def _jump_velocity(self):
        """Launch velocity for a self-initiated jump: a push away from the current
        edge, plus a little drift along it in the direction it was already facing."""
        along = self.direction * self.JUMP_ALONG_SPEED
        dx, dy = self._inward_delta(self.JUMP_INWARD_SPEED)
        if self.edge in ("bottom", "top"):
            return along, dy
        return dx, along

    def _begin_jump(self):
        self.vx, self.vy = self._jump_velocity()
        self._tumble = self._tumble_frames()
        self._tumble_idx = 0
        self.state = self.JUMPING

    def _on_jump_settle(self):
        # Jumping off the floor can land back on the floor; jumping off any other
        # edge is a fall, and gravity always wins - it comes down on the bottom edge.
        self._begin_return_to_edge(None if self.edge == "bottom" else "bottom")

    def _begin_climb(self):
        """From the floor, scurry up onto a random wall or the ceiling instead of
        jumping in place - the only way back off the bottom edge once it's landed
        there, since a fall (see _on_jump_settle) only ever goes the other way."""
        self._begin_return_to_edge(forced_edge=random.choice(("top", "left", "right")))

    def _tick_airborne(self, on_settle, use_monitor_bounds=False):
        """Shared free-fall physics for both a drag-throw and a self-initiated jump:
        gravity, air friction, bouncing off screen bounds, and a tumbling spin -
        `on_settle` is called once speed drops low enough to stop.

        A throw bounces off the OUTER bounds of the whole virtual desktop (a hard
        enough flick can sail from one monitor to another - see BOUNCE_DAMPING above).
        A self-initiated jump instead uses `use_monitor_bounds` to stay within
        whichever single monitor it's currently over: monitors are commonly different
        sizes, and the combined bounding box has empty space below any monitor that
        doesn't reach as far down as the tallest one - a jump falling into that gap
        would drift and settle on a monitor it never actually jumped from.
        """
        self.vx *= self.AIR_FRICTION
        self.vy = self.vy * self.AIR_FRICTION + self.GRAVITY
        self.x += self.vx
        self.y += self.vy

        left, top, right, bottom = self._current_monitor_rect() if use_monitor_bounds else self._virtual_bounds()
        if self.x < left:
            self.x, self.vx = left, -self.vx * self.BOUNCE_DAMPING
        elif self.x + self.w > right:
            self.x, self.vx = right - self.w, -self.vx * self.BOUNCE_DAMPING
        if self.y < top:
            self.y, self.vy = top, -self.vy * self.BOUNCE_DAMPING
        elif self.y + self.h > bottom:
            self.y, self.vy = bottom - self.h, -self.vy * self.BOUNCE_DAMPING
        self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

        speed = math.hypot(self.vx, self.vy)
        spin = max(1, int(speed / 6))
        self._tumble_idx = (self._tumble_idx + spin) % len(self._tumble)
        self._set_image(self._tumble[self._tumble_idx])

        if speed < self.THROW_SETTLE_SPEED:
            on_settle()

    # -- dragging -------------------------------------------------------

    def _on_press(self, event):
        self.dragging = True
        self._drag_offset = (event.x, event.y)
        self._drag_history = [(time.perf_counter(), event.x_root, event.y_root)]

    def _on_drag(self, event):
        dx, dy = self._drag_offset
        self.x = float(event.x_root - dx)
        self.y = float(event.y_root - dy)
        self.win.geometry(f"+{int(self.x)}+{int(self.y)}")
        self._set_image(self.idle_frame)

        now = time.perf_counter()
        self._drag_history.append((now, event.x_root, event.y_root))
        cutoff = now - self.VELOCITY_WINDOW_S
        self._drag_history = [h for h in self._drag_history if h[0] >= cutoff]

    def _on_release(self, event):
        self.dragging = False
        if self._is_caged():
            # Re-home the cage to wherever it was just dropped, so dragging a caged
            # sprite onto a different monitor moves its cage there too.
            self.lock_to_current_monitor()
        vx, vy = self._flick_velocity()
        if math.hypot(vx, vy) >= self.THROW_MIN_SPEED:
            self._begin_throw(vx, vy)
        else:
            self._begin_return_to_edge()

    def _flick_velocity(self):
        """Sprite px/tick velocity from however the mouse moved just before release."""
        hist = getattr(self, "_drag_history", [])
        if len(hist) < 2:
            return 0.0, 0.0
        t0, x0, y0 = hist[0]
        t1, x1, y1 = hist[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0, 0.0
        scale = self.THROW_VELOCITY_SCALE
        vx = max(-self.MAX_THROW_SPEED, min(self.MAX_THROW_SPEED, (x1 - x0) / dt * scale))
        vy = max(-self.MAX_THROW_SPEED, min(self.MAX_THROW_SPEED, (y1 - y0) / dt * scale))
        return vx, vy

    def _on_right_click(self, event):
        self.close()

    # -- pause/resume (from the tray menu) -----------------------------

    def pause(self):
        self.paused = True
        self.win.withdraw()

    def resume(self):
        self.paused = False
        self.win.deiconify()

    # -- closing (right-click, or the whole app exiting) -----------------

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_close is not None:
            self.on_close(self)

    # -- main loop -------------------------------------------------------

    def tick(self):
        if self.closed:
            return
        if self.dragging or self.paused:
            self.win.after(self.FRAME_MS, self.tick)
            return

        if self.state == self.WALKING:
            self._advance_along_edge(self.WALK_SPEED)
            self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

            self._walk_idx = (self._walk_idx + 1) % len(self.walk_frames)
            self._set_image(self.walk_frames[self._walk_idx])

            self._state_ticks_left -= 1
            if self._state_ticks_left <= 0:
                self._decide_next_state()

        elif self.state == self.HOPPING:
            self._advance_along_edge(self.HOP_SPEED)
            self._hop_phase = (self._hop_phase + 1) % self.HOP_CYCLE_TICKS
            bob = self.HOP_HEIGHT * math.sin(math.pi * self._hop_phase / self.HOP_CYCLE_TICKS)
            dx, dy = self._inward_delta(bob)
            self.win.geometry(f"+{int(self.x + dx)}+{int(self.y + dy)}")

            self._walk_idx = (self._walk_idx + 1) % len(self.walk_frames)
            self._set_image(self.walk_frames[self._walk_idx])

            self._state_ticks_left -= 1
            if self._state_ticks_left <= 0:
                self._decide_next_state()

        elif self.state == self.JUMPING:
            self._tick_airborne(self._on_jump_settle, use_monitor_bounds=True)

        elif self.state == self.BODYCHECK:
            self._advance_along_edge(self.BODYCHECK_SPEED)
            self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

            self._walk_idx = (self._walk_idx + 1) % len(self.walk_frames)
            self._set_image(self.walk_frames[self._walk_idx])

            self._bodycheck_ticks_left -= 1
            if self._bodycheck_ticks_left <= 0:
                self.state = self.IDLE
                self._schedule_next_state()

        elif self.state == self.RETURNING:
            tx, ty = self._return_target
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dist <= self.RETURN_SPEED:
                self.x, self.y = tx, ty
                self.state = self.IDLE
                self._schedule_next_state()
            else:
                self.x += dx / dist * self.RETURN_SPEED
                self.y += dy / dist * self.RETURN_SPEED
            self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

            self._walk_idx = (self._walk_idx + 1) % len(self.walk_frames)
            self._set_image(self.walk_frames[self._walk_idx])

        elif self.state == self.THROWN:
            self._tick_airborne(self._begin_return_to_edge)

        else:  # IDLE
            if self._shake_queue:
                self._set_image(self._shake_queue.pop(0))
            elif self._wink_queue:
                self._set_image(self._wink_queue.pop(0))
            else:
                self._set_image(self.idle_frame)
                self._shake_ticks_left -= 1
                if self._shake_ticks_left <= 0:
                    self._shake_queue = list(self.shake_frames)
                    self._schedule_next_shake()
                self._wink_ticks_left -= 1
                if self._wink_ticks_left <= 0:
                    self._begin_wink()
                    self._schedule_next_wink()

            self._state_ticks_left -= 1
            if self._state_ticks_left <= 0:
                self._decide_next_state()

        self.win.after(self.FRAME_MS, self.tick)

    def _on_reassert_timer(self):
        if self.closed:
            return
        reassert_topmost(self.hwnd)
        # re-check in case a monitor was added/removed, resolution changed, or the
        # taskbar auto-hid, then clamp position back inside the (possibly new) bounds
        self.monitors = get_all_monitors()
        if not self.dragging and self.state not in (self.RETURNING, self.THROWN, self.JUMPING):
            lo, hi = self._edge_range()
            if self.edge in ("bottom", "top"):
                self.x = min(max(self.x, lo), hi)
            else:
                self.y = min(max(self.y, lo), hi)
            self._snap_fixed_coord()
            self.win.geometry(f"+{int(self.x)}+{int(self.y)}")
        self.win.after(self.REASSERT_MS, self._on_reassert_timer)


# ---------------------------------------------------------------------------
# Exit paths: system tray icon and a global Ctrl+Shift+Q hotkey.
# The windows are click-through, so they can never receive keyboard focus.
# ---------------------------------------------------------------------------
def build_tray_icon(
    on_exit, on_toggle_pause, is_paused, on_spawn,
    on_toggle_jumping, is_jumping_enabled, on_toggle_caging, is_caged,
):
    icon_img = _add_outline(draw_soot_sprite(32), OUTLINE_COLOR)
    bg = Image.new("RGBA", icon_img.size, (0, 0, 0, 0))
    bg.paste(icon_img, (0, 0), icon_img)
    menu = pystray.Menu(
        pystray.MenuItem("Add Russgeist", lambda icon, item: on_spawn()),
        pystray.MenuItem("Paused", lambda icon, item: on_toggle_pause(), checked=lambda item: is_paused()),
        pystray.MenuItem(
            "Jumping", lambda icon, item: on_toggle_jumping(), checked=lambda item: is_jumping_enabled()
        ),
        pystray.MenuItem(
            "Cage to Monitor", lambda icon, item: on_toggle_caging(), checked=lambda item: is_caged()
        ),
        pystray.MenuItem("Exit Russgeist", lambda icon, item: on_exit()),
    )
    return pystray.Icon("Russgeist", bg, "Russgeist", menu)


def hotkey_listener(on_exit):
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_Q):
        print("[Russgeist] Could not register Ctrl+Shift+Q (already in use by another app).")
        return
    msg = wintypes.MSG()
    try:
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                on_exit()
                break
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


def main():
    ensure_asset()

    root = tk.Tk()
    root.withdraw()  # the root itself hosts no sprite; each one gets its own Toplevel

    monitors = get_all_monitors()
    sprites = []
    persisted = load_settings()
    paused_state = {"paused": persisted["paused"]}
    sprite_settings = {"jumping_enabled": persisted["jumping_enabled"], "caged": persisted["caged"]}

    def persist_settings():
        save_settings({
            "jumping_enabled": sprite_settings["jumping_enabled"],
            "caged": sprite_settings["caged"],
            "paused": paused_state["paused"],
            "sprite_count": len(sprites),
        })

    def spawn_sprite():
        s = SootSprite(
            root, random.randint(*SPRITE_SIZE_RANGE), monitors, on_close=remove_sprite,
            settings=sprite_settings, sprites=sprites,
        )
        if paused_state["paused"]:
            s.pause()
        sprites.append(s)
        persist_settings()

    def remove_sprite(sprite):
        if sprite in sprites:
            sprites.remove(sprite)
        persist_settings()

    for _ in range(max(0, persisted["sprite_count"])):
        spawn_sprite()

    tray_icon_holder = {}

    def shutdown():
        icon = tray_icon_holder.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass

    def request_exit():
        root.after(0, shutdown)

    def toggle_pause():
        paused_state["paused"] = not paused_state["paused"]
        persist_settings()

        def apply():
            for s in sprites:
                s.resume() if not paused_state["paused"] else s.pause()
            icon = tray_icon_holder.get("icon")
            if icon is not None:
                icon.update_menu()

        root.after(0, apply)

    def toggle_jumping():
        sprite_settings["jumping_enabled"] = not sprite_settings["jumping_enabled"]
        persist_settings()
        icon = tray_icon_holder.get("icon")
        if icon is not None:
            icon.update_menu()

    def toggle_caging():
        sprite_settings["caged"] = not sprite_settings["caged"]
        persist_settings()

        def apply():
            if sprite_settings["caged"]:
                # Lock every sprite to wherever it happens to be right now; from then
                # on, dropping a caged sprite (drag-and-drop) re-homes it individually.
                for s in sprites:
                    s.lock_to_current_monitor()
            icon = tray_icon_holder.get("icon")
            if icon is not None:
                icon.update_menu()

        root.after(0, apply)

    if HAVE_PYSTRAY:
        tray_icon_holder["icon"] = build_tray_icon(
            request_exit, toggle_pause, lambda: paused_state["paused"],
            lambda: root.after(0, spawn_sprite),
            toggle_jumping, lambda: sprite_settings["jumping_enabled"],
            toggle_caging, lambda: sprite_settings["caged"],
        )
        threading.Thread(target=tray_icon_holder["icon"].run, daemon=True).start()
    else:
        print("[Russgeist] pystray not installed - tray exit/pause/spawn disabled. "
              "Install with 'pip install pystray', or use Ctrl+Shift+Q to quit.")

    threading.Thread(target=hotkey_listener, args=(request_exit,), daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
