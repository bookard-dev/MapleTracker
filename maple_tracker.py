"""
MapleTracker - EXP and meso tracker overlay for MapleStory (Classic).

  * EXP: read from the EXP number on the status bar ("EXP. 17,816 [88.13%]"). That number is the
    game's own running total, so every source of EXP counts exactly once - kills, the yellow party
    bonus, quests - on any map. Level-ups are handled (the EXP needed for the level is worked out
    from the number and the percentage).
  * Mesos: read from the pickup messages in the bottom-right corner ("You have gained mesos (+25)").
    The text is isolated from whatever map is behind it (bright pixels with the game's dark drop
    shadow), read with Windows' built-in OCR, and each message is counted once as it arrives.

It only takes screenshots of the game window. It never reads game memory, injects anything, or sends
input to the game. Its own windows are excluded from screenshots, so they never confuse the reader.

No hotkeys, no keyboard hooks: the overlay's own buttons (gear = tracker window, play / pause,
stop) do it. Mouse: drag the overlay to move it; right-click it for the tracker window too.
"""
import asyncio
import html
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import uuid

import cv2
import mss
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_FLAG = os.path.join(HERE, "open_window.flag")     # left by a second start: open the window
CONFIG_PATH = os.path.join(HERE, "tracker_config.json")
STATE_PATH = os.path.join(HERE, "tracker_state.json")
GLYPHS_PATH = os.path.join(HERE, "statusbar_glyphs.npz")
PROFILES_DIR = os.path.join(HERE, "profiles")             # one file of logs per character
PRICES_PATH = os.path.join(HERE, "prices.json")           # your item prices, shared by all characters

DEFAULTS = {
    "scan_hz": 7,                   # message-area screenshots per second
    "game_base_height": 768,        # the game's internal height; its UI is scaled from this
    "panel_x": None,                # overlay position on screen (None = left side of the game)
    "panel_y": None,
    "opacity": 0.9,
    "hide_when_game_not_focused": True,
    "close_with_game": True,        # quit when MapleStory closes (after close_after_s seconds)
    "close_after_s": 10,
    "debug": False,                 # print every reading / message to the console
    "hide_from_screenshots": False, # True: leave our windows out of ALL screen capture (yours too:
                                    # Snipping Tool, OBS, Discord), so they can sit on the areas read
    "trackers": {"exp": True, "meso": True, "loot": True, "potions": True},   # each can be switched off
    # what PROFIT counts (mesos in, potion cost out, plus these kinds of loot)
    "profit": {"etc": True, "equip": True, "scroll": True, "use": True, "setup": True,
               "free_potions": True},     # looted potions you also drink offset their cost
    "niadb_online": False,          # look things up on NiaDB (only with their permission);
                                    # off: only what's already saved in meowdb_cache/ is used
    "auto_profile": True,           # follow the character name on the status bar
    # the class / name block on the status bar, from the bottom-centre of the game
    "name_text": {"dx0": -322, "dx1": -190, "dy0": 34, "dy1": 3},
    # quick slots (potion counts), in game units from the bottom-centre of the game: the inner
    # box of the top-left slot, and the distance between slots
    "quickslots": {"dx": 258.67, "dy": 171.2, "pitch_x": 35.15, "pitch_y": 34.13},
    "native_size": None,            # the game's own resolution (None: 1366x768 for 16:9 windows)
    "overlay_scale": None,          # None = follow the screen size
    "window_scale": None,
    # remembered between runs
    "profile": None,                # the character whose logs are shown
    "last_map": "",
    "controls_geometry": None,      # tracker window position
    "window_tab": "tracker",
    "log_expanded": [],
    "overlay_loot_open": False,
    "underlay": "off",              # a panel behind the overlay text: "off", "light" or "dark"
    "underlay_opacity": 0.55,       # 0..1, set with the slider on the SETTINGS tab
    "exp_display": "exp",           # the overlay's EXP rate: "exp" (EXP/h), "pct" (level %/h) or "both"     # the overlay's loot grid (click LOOT on the overlay)             # maps opened on the LOG tab
    # where things are on screen, in game units (screen px / (client height / 768)):
    # EXP number on the status bar, measured from the bottom-centre of the game
    "exp_text": {"dx0": 62, "dx1": 172, "dy0": 31, "dy1": 20},
    # pickup message stack, measured from the bottom-right corner of the game
    "msg_area": {"width": 270, "top": 252, "bottom": 140},
    "item_area_width": 340,         # item names run longer: read them from a wider strip
    "msg_line_h": 13.9,             # distance between two message lines
}

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


LOG_PATH = os.path.join(HERE, "tracker_log.txt")


def log(*a):
    """Print, and append to tracker_log.txt (pythonw has no console, and how the .bat's output
    redirect behaves depends on how it was launched)."""
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------- config / state
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    old = load_json(CONFIG_PATH, {})
    if (old.get("msg_area") or {}).get("width") == 340:
        old["msg_area"]["width"] = 270         # (briefly widened; items now have their own strip)
    for k, v in old.items():
        if k in cfg:
            if isinstance(cfg[k], dict) and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ----------------------------------------------------------------------------- game window
def find_game_window():
    return user32.FindWindowW(None, "MapleStory") or None


def game_process_running():
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wt.HANDLE(-1).value:
        return True
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower().startswith("maplestory"):
                return True
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        k32.CloseHandle(snap)


def client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    p = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y, r.right, r.bottom


def exclude_from_capture(hwnd, on):
    """Optionally keep our own windows out of every screenshot (Windows 10 2004+), so the overlay
    could sit even on top of the text it reads. Off by default: it also hides them from the
    user's own screenshots and recordings."""
    if not on:
        return
    try:
        user32.SetWindowDisplayAffinity(hwnd, 0x11)        # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def top_hwnd(widget):
    return user32.GetParent(widget.winfo_id()) or widget.winfo_id()


# ----------------------------------------------------------------------------- status bar EXP
def glyph_masks(bgr):
    """White digits and the green [ ] brackets of the status bar numbers."""
    c = bgr.astype(np.int16)
    b, g, r = c[..., 0], c[..., 1], c[..., 2]
    lum = (b + g + r) // 3
    sat = c.max(axis=2) - c.min(axis=2)
    return (lum > 150) & (sat < 60), (g > 150) & (g - b > 60)


def segments(mask):
    """Runs of columns that contain any glyph pixels."""
    cols = mask.any(axis=0)
    out, x, n = [], 0, len(cols)
    while x < n:
        if cols[x]:
            x0 = x
            while x < n and cols[x]:
                x += 1
            out.append((x0, x))
        else:
            x += 1
    return out


def norm_glyph(m):
    """A glyph mask scaled to 12 px high (keeping its width) and centred on a 12x16 canvas, so
    templates captured at one resolution match at another and a '1' never looks like a '7'."""
    h, w = m.shape
    nw = max(1, min(16, int(round(w * 12.0 / h))))
    g = cv2.resize(m.astype(np.float32), (nw, 12), interpolation=cv2.INTER_AREA)
    out = np.zeros((12, 16), np.float32)
    x0 = (16 - nw) // 2
    out[:, x0:x0 + nw] = g
    return out


class ExpReader:
    """Reads 'EXP. 17,816 [88.13%]' off the status bar."""

    DIGIT_H = 7.47          # digit height in game units

    def __init__(self, path=GLYPHS_PATH):
        self.tpl, self.lab = None, None
        if os.path.exists(path):
            d = np.load(path)
            self.tpl, self.lab = d["templates"], [str(x) for x in d["labels"]]

    def classify(self, m):
        g = norm_glyph(m)
        dist = np.abs(self.tpl - g).mean(axis=(1, 2))
        i = int(np.argmin(dist))
        return (self.lab[i], float(dist[i]))

    def read(self, bgr, s):
        """-> (exp, percent) or None if the strip doesn't read cleanly."""
        if self.tpl is None:
            return None
        white, green = glyph_masks(bgr)
        m = white | green
        dh = self.DIGIT_H * s
        toks = []
        for x0, x1 in segments(m):
            ys = np.nonzero(m[:, x0:x1].any(axis=1))[0]
            y0, y1 = ys[0], ys[-1] + 1
            h, w = y1 - y0, x1 - x0
            if h > 1.25 * dh:
                toks.append("|")            # the 'EXP.' label or the bar's frame
                continue
            if green[y0:y1, x0:x1].sum() > white[y0:y1, x0:x1].sum():
                toks.append("[")            # [ or ], told apart by position below
                continue
            if h < 0.45 * dh:
                # comma ~0.29, point ~0.21 of a digit's height (a point sits on the old 0.21 line)
                toks.append("," if h > 0.25 * dh else ".")
                continue
            if h < 0.75 * dh or w > 1.25 * dh:
                toks.append("?")            # half a glyph, or two that touch: never guessed
                continue
            ch, dist = self.classify(m[y0:y1, x0:x1])
            toks.append(ch if dist <= 0.17 else "?")
        text = "".join(toks)
        mt = re.search(r"\|?[.,]*([\d,]+)\[(\d{1,3})[.,](\d\d)%\[", text)   # "11.93%": a point, never a comma
        if not mt:
            return None
        num = mt.group(1)
        if num.startswith(",") or ",," in num or num.endswith(","):
            return None
        exp = int(num.replace(",", ""))
        pct = int(mt.group(2)) + int(mt.group(3)) / 100.0
        if pct >= 100.0:
            return None
        return exp, pct


class ExpWatcher:
    """Turns status bar readings into confirmed EXP gains, including across level-ups. It keeps
    following the number all the time (paused or not), so resuming never counts EXP gained while
    paused; the session decides whether a gain counts."""

    def __init__(self):
        self.last = None            # (exp, pct) last accepted reading
        self.need_lo = 0.0          # EXP needed for this level lies in [need_lo, need_hi]
        self.need_hi = float("inf")
        self.cand, self.cand_n = None, 0
        self.rejects = 0            # confirmed readings in a row that didn't add up
        self.seen = None            # when the number last read cleanly

    REJECTS_TO_RESTART = 3

    def _reject(self, reading):
        """A steady reading that doesn't fit. A few in a row mean what we remember is wrong
        (another character logged in, or a misread got learned): start over from here."""
        self.rejects += 1
        if self.rejects < self.REJECTS_TO_RESTART:
            return 0, 0, None
        self.rejects = 0
        self.last = reading
        self.need_lo, self.need_hi = 0.0, float("inf")
        self._learn(*reading)
        return 0, 0, "the EXP number stopped adding up (another character?) - following it again from here"

    @staticmethod
    def need_range(exp, pct):
        """The game shows the percentage cut to 2 decimals: exp/need*100 is in [pct, pct+0.01)."""
        if pct <= 0:
            return 0.0, float("inf")
        return exp * 100.0 / (pct + 0.01), exp * 100.0 / pct

    @property
    def need(self):
        if self.need_hi == float("inf") or self.need_lo <= 0:
            return None
        return (self.need_lo + self.need_hi) / 2

    def _fits(self, exp, pct):
        lo, hi = self.need_range(exp, pct)
        return max(lo, self.need_lo) <= min(hi, self.need_hi) * 1.0005 + 1

    def _learn(self, exp, pct):
        lo, hi = self.need_range(exp, pct)
        self.need_lo, self.need_hi = max(self.need_lo, lo), min(self.need_hi, hi)

    GAP_S = 8.0                 # no readings this long: loading, character select, alt-tabbed...

    def update(self, reading, now=None):
        """Feed a reading -> (EXP gained, levels gained, description or None)."""
        none = (0, 0, None)
        now = time.monotonic() if now is None else now
        gap = self.seen is not None and now - self.seen > self.GAP_S
        self.seen = now
        if gap and self.last is not None and reading[0] < self.last[0]:
            # EXP is lower after a while without readings: that's another character (or a death
            # we didn't see), never a level-up - follow the new number without counting anything
            self.cand, self.cand_n = None, 0
            self.last = None
            self.need_lo, self.need_hi = 0.0, float("inf")
            self.rejects = 0
        if reading != self.cand:
            self.cand, self.cand_n = reading, 1
            return none
        self.cand_n += 1
        exp, pct = reading
        dropped = self.last is not None and exp < self.last[0]
        # act once, when a reading has been seen twice in a row (4 times for a drop in EXP,
        # which is rare: a level-up or a death)
        if self.cand_n != (4 if dropped else 2):
            return none
        if self.last is None:
            self.last = reading
            self._learn(exp, pct)
            return none
        lexp, lpct = self.last
        if exp == lexp:
            return none
        if dropped and pct >= lpct:
            return self._reject(reading)         # less EXP but a higher percentage: a misread
        if exp > lexp:
            if pct < lpct or not self._fits(exp, pct):
                return self._reject(reading)     # doesn't add up: a misread, ignore it
            self.rejects = 0
            self.last = reading
            self._learn(exp, pct)
            return exp - lexp, 0, f"+{exp - lexp} EXP"
        self.rejects = 0
        # EXP went down: a level-up (EXP needed changes) or a death (it doesn't)
        need_old = self.need
        level_up = not self._fits(exp, pct) or (pct < 1.0 and lpct > 11.0)
        self.last = reading
        if level_up and need_old:
            carried = max(0, int(round(need_old - lexp)))
            self.need_lo, self.need_hi = 0.0, float("inf")
            self._learn(exp, pct)
            return carried + exp, 1, f"level up! +{carried + exp} EXP"
        if level_up:                             # never knew the old level's size: just re-base
            self.need_lo, self.need_hi = 0.0, float("inf")
            self._learn(exp, pct)
            return 0, 1, "EXP went down (level-up before the size of the level was known)"
        self._learn(exp, pct)
        return 0, 0, f"EXP lost ({lexp - exp}), not counted"


# ----------------------------------------------------------------------------- pickup messages
def text_mask(bgr, k):
    """Fresh message text: near-white pixels with the game's darker drop shadow below-right.
    Works on any map: bright scenery has no shadow, and faded old messages aren't bright."""
    c = bgr.astype(np.int16)
    lum = c.sum(axis=2) // 3
    sat = c.max(axis=2) - c.min(axis=2)
    bright = (lum > 185) & (sat < 45)
    shadow = np.full_like(lum, 255)
    for d in sorted({1, k}):
        sh = np.full_like(lum, 255)
        sh[:-d, :-d] = lum[d:, d:]
        shadow = np.minimum(shadow, sh)
    return bright & (shadow < lum - 90)


class Ocr:
    """Windows' built-in OCR (Windows.Media.Ocr) on a black-on-white image."""

    def __init__(self):
        self.engine, self.error = None, None
        try:
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
            from winrt.windows.storage.streams import DataWriter
            from winrt.windows.globalization import Language
        except ImportError:
            self.error = "OCR bindings missing: pip install winrt-Windows.Media.Ocr (see README)"
            return
        self._SB, self._PF, self._DW = SoftwareBitmap, BitmapPixelFormat, DataWriter
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None or not eng.recognizer_language.language_tag.lower().startswith("en"):
            for tag in ("en-US", "en-GB"):
                try:
                    if OcrEngine.is_language_supported(Language(tag)):
                        eng = OcrEngine.try_create_from_language(Language(tag))
                        break
                except Exception:
                    pass
        if eng is None:
            self.error = "No Windows OCR language installed (Settings > Language > English > OCR)"
            return
        self.engine = eng
        self.loop = asyncio.new_event_loop()

    def lines(self, gray):
        """-> [(text, x, y, w, h)] in the image's pixels."""
        h, w = gray.shape
        bgra = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGRA)
        dw = self._DW()
        dw.write_bytes(bgra.tobytes())
        bmp = self._SB.create_copy_from_buffer(dw.detach_buffer(), self._PF.BGRA8, w, h)
        res = self.loop.run_until_complete(self.engine.recognize_async(bmp))
        out = []
        for ln in res.lines:
            rs = [wd.bounding_rect for wd in ln.words]
            x0, y0 = min(r.x for r in rs), min(r.y for r in rs)
            x1, y1 = max(r.x + r.width for r in rs), max(r.y + r.height for r in rs)
            out.append((ln.text, x0, y0, x1 - x0, y1 - y0))
        return out


# "You have gained mesos (+25)". OCR sometimes misspells the faded/odd letters ("rnesos", "mesas")
# and reads this font's 9 as "g", so: find the (+amount) at the end, call it a meso line when it
# says gained/mesos and isn't an EXP line, and map look-alike letters in the amount back to digits.
AMOUNT_RE = re.compile(r"\(\s*[.,]?\+\s*([0-9gqoODlI|BSZ, ]+?)\s*[)\]j]?\s*$")
MESO_WORD_RE = re.compile(r"gain|m\S?s\S?s|rn\S?s", re.I)
NOT_MESO_RE = re.compile(r"rec\S?i|EXP|E.P\b|fame", re.I)
DIGIT_FIX = str.maketrans("gqoODlI|BSZ", "99000111852")


def meso_amount(text):
    """The amount of a meso pickup message, or None if the line isn't one (or doesn't read)."""
    m = AMOUNT_RE.search(text.strip())
    if not m or not MESO_WORD_RE.search(text) or NOT_MESO_RE.search(text):
        return None
    raw = m.group(1).replace(" ", "").translate(DIGIT_FIX)
    if not re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", raw):
        return None
    amt = int(raw.replace(",", ""))
    return amt if 0 < amt < 100_000_000 else None


# "Gold Ore x1 earned. (Etc)" - OCR often garbles the "x1" ("Kl", "gl", "B.I") and the brackets
EARNED_RE = re.compile(r"\s([eabgp]?[ae]?[ae]?[rm][rnm]?[a-z.\]\[|}\-'\"æ ]{0,6}?\.?)\s*"
                       r"((?:\(|\{|OJ|0-|I-I).{0,8})?$", re.I)
QTY_JUNK = {"kl", "ki", "gl", "gi", "hi", "hl", "b.i", "v.i", "r.l", "xl", "xi", "x|", "{1", "(1", "ri", "yi", "y1"}
CATS = {"e": "Etc", "u": "Use", "s": "Setup", "q": "Equip"}


def item_pickup(text):
    """(name as read, quantity, category or None) for an item pickup line, else None."""
    text = text.strip()
    if "meso" in text.lower() or MESO_WORD_RE.search(text) and AMOUNT_RE.search(text):
        return None
    m = EARNED_RE.search(text)
    if not m or m.start() < 2:
        return None
    name = text[:m.start()].strip()
    qty = 1
    # "x14" read as "Kl 4" / "x1 4": a garbled x1 followed by the rest of the number
    m14 = re.search(r"\s(\S{0,3}[1lI|])\s(\d{1,3})$", name)
    if m14 and re.search(r"[^A-Za-z]|^[xXKkRrBbgHhVY][lI1]$", m14.group(1)):
        name = name[:m14.start()] + " x1" + m14.group(2)
    parts = name.rsplit(" ", 1)
    if len(parts) == 2:
        tok = parts[1].strip()
        m12 = re.fullmatch(r"([^\s]{0,3}?)[1lI|](\d{1,2})", tok)
        if m12 and not re.fullmatch(r"[xX×]?\d{1,3}", tok) and (re.search(r"[^A-Za-z]", m12.group(1))
                                                                 or re.fullmatch(r"[A-Za-z]?", m12.group(1))):
            qty = int("1" + m12.group(2))
            name = parts[0]
        elif re.fullmatch(r"[xX×]?[0-9lI|]{1,3}", tok) and re.search(r"\d", tok):
            qty = int(re.sub(r"\D", "", tok.replace("l", "1").replace("I", "1").replace("|", "1")) or 1)
            name = parts[0]
        elif tok.lower() in QTY_JUNK or (len(tok) <= 3 and re.fullmatch(r"[xX][^A-Za-z]{0,2}", tok))                 or (len(tok) <= 5 and re.fullmatch(r"[^\s]{0,4}[1lI|]", tok) and re.search(r"[^A-Za-z]", tok))                 or re.fullmatch(r"[A-Z][lI1]", tok):
            name = parts[0]
    cat = None
    if m.group(2):
        c = re.sub(r"[^a-z]", "", m.group(2).lower())
        if c.startswith("eq"):
            cat = "Equip"
        elif re.search(r"se|sa|ilse|ijse", c) and not c.startswith("et"):
            cat = "Use"
        elif c.startswith("se"):
            cat = "Setup"
        elif c:
            cat = CATS.get(c[0])
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)          # stray marks before the name
    name = re.sub(r"\s+", " ", name).strip()
    truncated = name.endswith("...") or name.endswith("…") or name.endswith("..")
    name = name.rstrip(".… ")
    if len(name) < 3:
        return None
    return name + ("..." if truncated else ""), max(1, min(qty, 9999)), cat


class MessageReader:
    """Follows the pickup message stack and reports each meso message once.

    New messages appear in the bottom line and push older ones up a line. Each frame the whole
    stack's upward shift is measured (by sliding last frame's text over this one's), every line is
    followed by position, and a line that wasn't there before is a new message. A message is
    counted once its amount has read the same twice, so a misread digit can't sneak in.
    """
    PAD = 16

    def __init__(self, ocr, canon=None):
        self.ocr = ocr
        self.canon = canon or (lambda name: name)     # snaps an OCR'd item name to the real one
        self.prev = None              # last frame's blurred text mask (half size)
        self.tracks = []              # {"y", "seen", "votes", "counted", "text"}

    def _shift(self, cur, maxd):
        """How far (px, half size) the text moved up since the previous frame."""
        prev = self.prev
        if prev is None or prev.shape != cur.shape or not prev.any():
            return 0
        h = cur.shape[0]
        best, best_d = None, 0
        for d in range(0, min(maxd, h - 4) + 1):
            a, b = prev[d:], cur[:h - d]
            ink = (a + b) > 0.05
            if ink.sum() == 0:
                continue
            cost = np.abs(a - b)[ink].mean() + 0.002 * d
            if best is None or cost < best:
                best, best_d = cost, d
        return best_d

    def read_lines(self, bgr, s):
        """-> (lines [(y, text)] top to bottom, how far the stack scrolled up since the last frame
        in px, whether there's any text at all)."""
        k = max(1, int(round(s)))
        m = text_mask(bgr, k)
        half = cv2.resize(m.astype(np.float32), (m.shape[1] // 2, m.shape[0] // 2), interpolation=cv2.INTER_AREA)
        half = cv2.GaussianBlur(half, (3, 3), 0)
        lh = DEFAULTS["msg_line_h"] * s
        d = 2 * self._shift(half, int(5 * lh / 2))
        self.prev = half
        if not m.any():
            return [], d, False
        up = max(1.0, 1.875 / s)
        mm = cv2.dilate(m.astype(np.uint8), np.ones((2, 2), np.uint8))
        img = np.where(mm > 0, 0, 255).astype(np.uint8)
        if up > 1.0:
            img = cv2.resize(img, None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
        img = cv2.copyMakeBorder(img, self.PAD, self.PAD, self.PAD, self.PAD, cv2.BORDER_CONSTANT, value=255)
        found = []
        for text, x, y, w, h in self.ocr.lines(img):
            y = (y - self.PAD) / up
            if h / up >= 0.4 * lh:
                found.append((y, text))
        return sorted(found), d, True

    def update(self, bgr, s, now):
        """-> list of meso amounts that newly arrived."""
        if self.ocr.engine is None:
            return []
        k = max(1, int(round(s)))
        m = text_mask(bgr, k)
        half = cv2.resize(m.astype(np.float32), (m.shape[1] // 2, m.shape[0] // 2),
                          interpolation=cv2.INTER_AREA)
        half = cv2.GaussianBlur(half, (3, 3), 0)
        lh = DEFAULTS["msg_line_h"] * s
        d = 2 * self._shift(half, int(5 * lh / 2))
        self.prev = half
        for t in self.tracks:
            t["y"] -= d
        if not m.any():
            return self._expire(now, lh)
        # OCR wants text ~20 px high; the game's is ~11 game units
        up = max(1.0, 1.875 / s)
        mm = cv2.dilate(m.astype(np.uint8), np.ones((2, 2), np.uint8))
        img = np.where(mm > 0, 0, 255).astype(np.uint8)
        if up > 1.0:
            img = cv2.resize(img, None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
        img = cv2.copyMakeBorder(img, self.PAD, self.PAD, self.PAD, self.PAD,
                                 cv2.BORDER_CONSTANT, value=255)
        found = []
        for text, x, y, w, h in self.ocr.lines(img):
            y = (y - self.PAD) / up
            if h / up < 0.4 * lh:
                continue                     # specks, not a line of text
            found.append((y, text))
        new = []
        free = list(self.tracks)
        for y, text in sorted(found):
            t = min(free, key=lambda t: abs(t["y"] - y), default=None)
            if t is None or abs(t["y"] - y) > 0.5 * lh:
                t = {"y": y, "seen": now, "votes": {}, "counted": set(), "text": text}
                self.tracks.append(t)
            else:
                free.remove(t)
            t["y"], t["seen"], t["text"] = y, now, text
            amt = meso_amount(text)
            if amt is not None:
                key = ("meso", amt)
            else:
                pick = item_pickup(text)
                if pick is None:
                    continue
                name, qty, cat = pick
                raw = name
                name = self.canon(raw)
                if not name and " " in raw.rstrip(". "):
                    name = self.canon(raw.rsplit(" ", 1)[0] + ("..." if raw.endswith("...") else ""))
                if not name:
                    continue                  # OCR junk that matches no real item
                key = ("item", name, qty)
                done = t.setdefault("done", set())
                if "item" in t["counted"] and name not in done:
                    # a different item on this line: the stack scrolled and this is a new message
                    # (the scroll estimate lost its place). Start over on it, remembering what
                    # this line already counted so a one-off misread can't count anything twice.
                    old = t
                    t = {"y": y, "seen": now, "votes": {}, "counted": set(), "text": text, "done": set(done)}
                    self.tracks[self.tracks.index(old)] = t
                if name in t["done"]:
                    continue
                if cat:
                    t.setdefault("cats", {})[name] = cat
            # a line counts once per kind (mesos / item), once the same reading came up twice
            if key[0] in t["counted"]:
                continue
            t["votes"][key] = t["votes"].get(key, 0) + 1
            if t["votes"][key] >= 2:
                t["counted"].add(key[0])
                if key[0] == "item":
                    t.setdefault("done", set()).add(key[1])
                new.append(self._event(t, key))
        new += self._expire(now, lh)
        return new

    @staticmethod
    def _event(t, key):
        if key[0] == "item":
            return ("item", key[1], key[2], t.get("cats", {}).get(key[1], ""))
        return key

    def _expire(self, now, lh):
        """Forget lines not seen for a while or pushed out of the top of the area. A pickup line
        that was read as a real item only once (it scrolled away fast) still counts then."""
        keep, out = [], []
        for t in self.tracks:
            if now - t["seen"] < 8.0 and t["y"] > -lh:
                keep.append(t)
                continue
            if "item" not in t["counted"] and not t.get("done"):
                items = [(n, k) for k, n in t["votes"].items() if k[0] == "item"]
                if items:
                    out.append(self._event(t, max(items)[1]))
        self.tracks = keep
        return out


class ItemFeed(MessageReader):
    """Follows the message stack and reports each item pickup once.

    New messages always appear in the bottom line and push the others up one line. Each line is
    given a slot (lines above the bottom one) and a kind: EXP, mesos, an item (by name) or
    unreadable. Lining this frame's slots up with the last frame's says how many messages were
    pushed in since - the item names make that unambiguous, the measured scroll breaks ties - so
    each message keeps its identity while it scrolls up, and is counted exactly once: when its
    item name has read the same twice, or, if it only ever read once, when it leaves."""
    MAX_SLOTS = 12

    def __init__(self, ocr, canon=None):
        super().__init__(ocr, canon)
        self.y0 = None              # where the bottom line sits (px, in the strip)
        self.slots = {}             # slot -> record {"votes", "qty", "cat", "counted", "seen", "sig"}

    def _sig(self, text):
        if meso_amount(text) is not None:
            return ("meso",), None
        if re.search(r"rec\S?i\S{0,4}d?\]?\s+E", text) or re.search(r"E\S?P", text) and "(+" in text:
            return ("exp",), None
        pick = item_pickup(text)
        if pick is None:
            return None, None
        name, qty, cat = pick
        canon = self.canon(name)
        if not canon and " " in name.rstrip(". "):
            canon = self.canon(name.rsplit(" ", 1)[0] + ("..." if name.endswith("...") else ""))
        if not canon:
            return None, None
        return ("item", canon), (canon, qty, cat)

    @staticmethod
    def _best_qty(rec, name):
        """The largest quantity read at least twice (a digit is more often lost than invented),
        else the most common one."""
        qtys = rec["qty"].get(name, {1: 1})
        sure = [q for q, n in qtys.items() if n >= 2]
        return max(sure) if sure else max(qtys, key=qtys.get)

    def _event(self, rec):
        name = max(rec["votes"], key=rec["votes"].get)
        qty = self._best_qty(rec, name)
        rec["name"], rec["sent"] = name, qty
        return ("item", name, qty, rec["cat"].get(name, ""))

    def update(self, bgr, s, now):
        if self.ocr.engine is None:
            return []
        lh = DEFAULTS["msg_line_h"] * s
        lines, d, any_text = self.read_lines(bgr, s)
        out = []
        if lines:
            bottom = lines[-1][0]
            if self.y0 is None or bottom > self.y0 + 0.5 * lh:
                self.y0 = bottom
            elif abs(bottom - self.y0) < 0.5 * lh:
                self.y0 = 0.9 * self.y0 + 0.1 * bottom
        cur = {}
        for y, text in lines:
            pos = (self.y0 - y) / lh
            slot = int(round(pos))
            if abs(pos - slot) > 0.3:
                continue                    # caught mid-scroll, between two lines: wait for it to settle
            if 0 <= slot <= self.MAX_SLOTS:
                cur[slot] = self._sig(text)
        # how many new messages pushed in: line the slots up with the last frame's
        k_shift = int(round(d / lh))
        best = None
        for k in range(0, 6):
            score = 0.0
            for slot, (sig, _) in cur.items():
                old = self.slots.get(slot - k)
                if old is None or sig is None or old["sig"] is None:
                    continue
                score += 1.0 if old["sig"] == sig else -2.0
            if k == 0 and 0 in cur and cur[0][0] is not None and self.slots.get(0) is not None                     and self.slots[0]["sig"] not in (None, cur[0][0]):
                score -= 3.0                # what's in the bottom line changed: something new came in
            score += (0.6 if k == k_shift else 0.0) - 0.5 * k     # new messages need evidence
            if best is None or score > best[0] + 1e-9:
                best = (score, k)
        k = best[1] if self.slots else 0
        moved = {}
        for slot, rec in self.slots.items():
            if slot + k <= self.MAX_SLOTS:
                moved[slot + k] = rec
            else:
                out += self._leave(rec)
        for slot, (sig, pick) in cur.items():
            rec = moved.get(slot)
            if rec is None or (slot < k):
                rec = {"votes": {}, "qty": {}, "cat": {}, "counted": False, "seen": now, "sig": sig}
                moved[slot] = rec
            rec["seen"] = now
            if sig is not None:
                rec["sig"] = sig
            if pick is not None and rec["counted"] and pick[0] == rec.get("name"):
                # counted already: a clearer read of a bigger quantity adds the difference
                q = rec["qty"].setdefault(pick[0], {})
                q[pick[1]] = q.get(pick[1], 0) + 1
                best = self._best_qty(rec, pick[0])
                if best > rec.get("sent", best):
                    out.append(("item", pick[0], best - rec["sent"], rec["cat"].get(pick[0], "")))
                    rec["sent"] = best
            if pick is not None and not rec["counted"]:
                name, qty, cat = pick
                rec["votes"][name] = rec["votes"].get(name, 0) + 1
                rec["qty"].setdefault(name, {})[qty] = rec["qty"].get(name, {}).get(qty, 0) + 1
                if cat:
                    rec["cat"][name] = cat
                if rec["votes"][name] >= 2:
                    rec["counted"] = True
                    out.append(self._event(rec))
        # forget lines gone for a while (faded out)
        self.slots = {}
        for slot, rec in moved.items():
            if now - rec["seen"] < (2.0 if any_text else 8.0) * (1 + slot / 4):
                self.slots[slot] = rec
            else:
                out += self._leave(rec)
        return out

    def _leave(self, rec):
        """A pickup read as a real item only once still counts when its line goes."""
        if rec["counted"] or not rec["votes"]:
            return []
        rec["counted"] = True
        return [self._event(rec)]


# ----------------------------------------------------------------------------- potions (quick slots)
# Common HP/MP items with MapleStory Classic's item IDs (they differ from old GMS data: Meat is
# 2010003 in Classic) and NPC shop prices, both from MeowDB (meowdb.com/msclassic). Icons are
# MeowDB's (cdn.meowdb.com/icons/<id>.png), cached in icons/. price None = not sold by NPCs.
CATALOG = [
    (2000000, "Red Potion", 50, "HP"), (2000001, "Orange Potion", 150, "HP"),
    (2000002, "White Potion", 350, "HP"), (2000003, "Blue Potion", 220, "MP"),
    (2000004, "Elixir", 3000, "HP+MP"), (2000005, "Power Elixir", None, "HP+MP"),
    (2000006, "Mana Elixir", None, "MP"),
    (2010001, "Apple", 20, "HP"), (2010002, "Egg", 30, "HP"), (2010003, "Meat", 80, "HP"),
    (2010004, "Orange", 50, "MP"), (2010005, "Lemon", 150, "MP"), (2010006, "Watermelon", 1920, "HP+MP"),
    (2020001, "Fried Chicken", 60, "HP"), (2020005, "Hot Dog", 180, "HP"), (2020007, "Dried Squid", 180, "HP"),
    (2020003, "Pizza", 252, "HP"), (2020004, "Hamburger", 350, "HP"), (2020006, "Hot Dog Supreme", 480, "HP"),
    (2022003, "Unagi", 1000, "HP"), (2020012, "Ice Cream Pop", 1500, "HP"),
    (2020000, "Salad", 220, "MP"), (2022000, "Pure Water", 1120, "MP"),
    (2020002, "Cake", 150, "HP+MP"), (2020013, "Red Bean Sundae", 2250, "HP+MP"),
]
ICON_DIR = os.path.join(HERE, "icons")
ICONS_ONLINE = False            # download missing icons (cdn.meowdb.com) - set from "niadb_online"
COUNT_GLYPHS_PATH = os.path.join(HERE, "count_glyphs.npz")
# quick slots are (column, row) in the 4 x 3 grid; their keys are each player's own


def slot_name(slot):
    return f"row {slot[1] + 1}, slot {slot[0] + 1}"
_ICONS = {}


def item_icon(item_id):
    """The item's icon (RGBA), from icons/ or downloaded from MeowDB once; None if unavailable."""
    if not item_id:
        return None
    if item_id in _ICONS:
        return _ICONS[item_id]
    path = os.path.join(ICON_DIR, f"{int(item_id)}.png")
    img = None
    if not os.path.exists(path) and not ICONS_ONLINE:
        return None                       # not saved, and not allowed to fetch it: no icon
    try:
        if not os.path.exists(path):
            import urllib.request
            os.makedirs(ICON_DIR, exist_ok=True)
            req = urllib.request.Request(f"https://cdn.meowdb.com/icons/{int(item_id)}.png",
                                         headers={"User-Agent": "MapleTracker"})
            data = urllib.request.urlopen(req, timeout=10).read()
            with open(path, "wb") as f:
                f.write(data)
        img = Image.open(path).convert("RGBA")
    except Exception as ex:
        log(f"no icon for item {item_id}: {ex}")
    _ICONS[item_id] = img
    return img


def slot_origin(col, row, rect, s, cfg):
    """Screen position of a quick slot's inner box. The quick slots sit at a fixed place relative
    to the status bar (bottom centre of the game), like the EXP number."""
    q = cfg["quickslots"]
    cx, cy, cw, ch = rect
    return (cx + cw / 2 + (q["dx"] + q["pitch_x"] * col) * s,
            cy + ch - (q["dy"] - q["pitch_y"] * row) * s)


def native_size(rect, cfg):
    """The size the game draws its picture at before stretching it to the window (1366x768 for a
    16:9 window); config "native_size" overrides it."""
    if cfg.get("native_size"):
        return tuple(cfg["native_size"])
    w, h = rect[2], rect[3]
    if h <= 0:
        return 1366, 768
    return (1366 if abs(w / h - 16 / 9) < 0.01 else int(round(768 * w / h))), 768


def _cover(n0, n, scale, p0, p):
    """(n x p) weights: game pixel n0+i covers screen [(n0+i)*scale, (n0+i+1)*scale); screen pixel
    p0+j covers [p0+j, p0+j+1); the weight is the overlap (so a game pixel = the average of the
    screen area it was stretched over)."""
    w = np.zeros((n, p), np.float32)
    for i in range(n):
        a, b = (n0 + i) * scale, (n0 + i + 1) * scale
        for j in range(max(0, int(np.floor(a)) - p0), min(p, int(np.ceil(b)) - p0)):
            ov = min(b, p0 + j + 1) - max(a, p0 + j)
            if ov > 0:
                w[i, j] = ov / scale
    return w


def to_native_gray(patch, px0, py0, sx, sy, nx0, ny0, nw, nh):
    """Brightness of game pixels [nx0, nx0+nw) x [ny0, ny0+nh), from a screenshot patch whose top
    left is at screen (px0, py0) (screen coordinates relative to the game's client area)."""
    g = patch.astype(np.float32).mean(axis=2)
    return _cover(ny0, nh, sy, py0, g.shape[0]) @ g @ _cover(nx0, nw, sx, px0, g.shape[1]).T


class CountReader:
    """Reads the item count drawn at the bottom-left of a quick slot.

    The game draws its UI at 1366x768 and stretches that to the window, which smears each digit
    differently depending on where it lands. So the count's corner is first turned back into the
    game's own pixels (exactly, by averaging the screen area each one was stretched over): there,
    every digit is a crisp 1-pixel-stroke shape that looks the same wherever it sits - first or
    third digit, 105 or 999. Each digit's template is the average of its examples, and only the
    pixels that are the same in every example count (the icon showing through varies), so the
    item behind the digits doesn't matter."""
    NW, NH = 26, 13               # the count's corner, in game pixels
    DX, DY = -3, 17               # its top-left, from the slot's inner box (game pixels)
    MAX_ERR, MIN_MARGIN = 0.16, 0.03
    PRIOR_BONUS = 0.0             # (reading on the game's grid is accurate enough without a
                                  # head start for "same as before, or a few less")
    SHIFTS = (0,)                 # on the game's own pixel grid, digits sit exactly where expected

    def __init__(self, path=COUNT_GLYPHS_PATH):
        self.by_char = {}         # char -> (template, confident-pixel mask, advance)
        self.start = 2
        if os.path.exists(path):
            d = np.load(path)
            if "native" in d.files:
                self.start = int(d["start"])
                for ch, tp, cf, w in zip(d["labels"], d["templates"], d["confident"], d["widths"]):
                    self.by_char[str(ch)] = (tp[:, :w], cf[:, :w], int(w))
        self.tpl = list(self.by_char)

    @classmethod
    def corner(cls, slot, rect, cfg, s):
        """Where a slot's count corner is: (game x, game y) of its top left, and the scale."""
        nw, nh = native_size(rect, cfg)
        sx, sy = rect[2] / nw, rect[3] / nh
        ox, oy = slot_origin(slot[0], slot[1], (0, 0, rect[2], rect[3]), s, cfg)
        return int(np.floor(ox / sx)) + cls.DX, int(np.floor(oy / sy)) + cls.DY, sx, sy

    @classmethod
    def grab_box(cls, nx0, ny0, sx, sy):
        """The screen box (relative to the client) that covers the count corner."""
        x0, y0 = int(np.floor(nx0 * sx)), int(np.floor(ny0 * sy))
        x1, y1 = int(np.ceil((nx0 + cls.NW) * sx)), int(np.ceil((ny0 + cls.NH) * sy))
        return x0, y0, x1, y1

    @classmethod
    def native(cls, patch, px0, py0, nx0, ny0, sx, sy):
        return to_native_gray(patch, px0, py0, sx, sy, nx0, ny0, cls.NW, cls.NH) / 255.0

    def digit_err(self, g, ch, x):
        """How badly digit ch fits near x: (error, where it fits, where the next digit goes)."""
        tp, cf, w = self.by_char[ch]
        best = (9.0, x, x + w)
        for xx in range(max(0, x + self.SHIFTS[0]), x + self.SHIFTS[-1] + 1):
            cell = g[:, xx:xx + w]
            if cell.shape[1] < w:
                cell = np.pad(cell, ((0, 0), (0, w - cell.shape[1])), constant_values=0.5)
            e = float(np.abs(cell - tp)[cf].mean())
            if e < best[0]:
                best = (e, xx, xx + w)
        return best

    def score(self, g, text):
        """(total digit error, worst digit error); a digit that seems to follow counts as bad."""
        x, errs = self.start, []
        for ch in text:
            if ch not in self.by_char:
                return 9.0, 9.0
            e, _, x = self.digit_err(g, ch, x)
            errs.append(e)
        if x < self.NW - 4:
            after = min(self.digit_err(g, ch, x)[0] for ch in self.by_char)
            if after < self.MAX_ERR:
                errs.append(1.0 - after)
        return sum(errs), max(errs)

    def free_read(self, g):
        x, digits = self.start, ""
        while len(digits) < 4 and x < self.NW - 4:
            (e, _, nx), ch = min((self.digit_err(g, ch, x), ch) for ch in self.by_char)
            if e > self.MAX_ERR:
                break
            digits += ch
            x = nx
        return digits

    def read(self, g, near=None):
        """g: the count corner in game pixels (0..1 brightness). -> the count, 0 for an empty
        slot, or None if it doesn't read cleanly. near: the last known count; it's nearly always
        that or a few less, so those numbers get a small head start (a real gain, like looting
        or buying, still wins when the digits clearly say so)."""
        if not self.by_char or g.size == 0:
            return None
        free = self.free_read(g)
        cands = {}
        for text, bonus in ([(free, 0.0)] if free else []) + \
                [(str(v), self.PRIOR_BONUS) for v in range(max(1, (near or 0) - 12), (near or -1) + 1)]:
            total, worst = self.score(g, text)
            if worst <= self.MAX_ERR:
                cands[text] = min(cands.get(text, 99.0), total - bonus)
        if not cands:
            if free:
                return None
            # nothing there: an empty slot is plain and bright where the digits would be
            area = g[1:-2, self.start:self.start + 14]
            return 0 if area.mean() > 0.85 and area.std() < 0.05 else None
        ranked = sorted(cands.items(), key=lambda kv: kv[1])
        if len(ranked) > 1 and ranked[1][1] - ranked[0][1] < self.MIN_MARGIN:
            return None                       # two numbers fit about as well: don't guess
        return int(ranked[0][0])


class PotionCounter:
    """Counts potions used from one slot's count. Only decreases count: a drop or a purchase
    raises the count and simply becomes the new baseline, so 80 -> 75 -> (loot) 76 -> 70 is
    5 + 6 = 11 used. A reading counts once it's been seen CONFIRM times in a row."""
    CONFIRM = 3
    MAX_STEP = 12            # a bigger drop at once is moving / dropping / selling the stack...
    BIG_CONFIRM = 20         # ...but only once it has stayed for ~6 s: a digit that flickers
                             # out for a moment (101 read as 10) never gets that far

    def __init__(self):
        self.last = None
        self.cand, self.cand_n = None, 0

    def update(self, value, now=None):
        """-> (used, note)."""
        if value is None:
            return 0, None
        if value != self.cand:
            self.cand, self.cand_n = value, 1
            return 0, None
        self.cand_n += 1
        # a big drop at once - or the slot suddenly looking empty (something may just be in front
        # of it) - has to stay put for a while before it's believed
        if self.last is not None and (self.last - value > self.MAX_STEP or (value == 0 and self.last > 1)):
            if self.cand_n != self.BIG_CONFIRM:
                return 0, None
            d, self.last = self.last - value, value
            return 0, f"count dropped by {d} and stayed there - not counted (moved, dropped or sold?)"
        if self.cand_n != self.CONFIRM:
            return 0, None
        if self.last is None:
            self.last = value
            return 0, None
        d, self.last = self.last - value, value
        if d <= 0:
            return 0, (f"count went up by {-d}" if d < 0 else None)
        return d, None


# ----------------------------------------------------------------------------- MeowDB (NiaDB)
# Map, monster and item data from NiaDB (meowdb.com/msclassic), used with the site's permission.
# Their rules for tools are followed: one request at a time, at least 10 s apart, the tool's name
# in the User-Agent, stop on 429 / 403, and NiaDB is credited wherever its data shows. Everything
# fetched is cached in meowdb_cache/, so each page is only ever fetched once (the map list and
# item index are refreshed weekly).
MEOW_BASE = "https://meowdb.com/msclassic"
MEOW_UA = "MapleTracker/1.0 (+https://github.com/bookard-dev/MapleTracker)"
MEOW_GAP_S = 10.0
MEOW_DIR = os.path.join(HERE, "meowdb_cache")
MEOW_CREDIT = "Data: NiaDB (meowdb.com)  ·  prices: MapleClassic Wiki (CC BY-NC-SA 4.0)"


def _next_payload(html_text):
    """The page's data, as Next.js embeds it in the HTML."""
    s = "".join(re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html_text, flags=re.S))
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.encode("utf-8", "replace").decode("unicode_escape", "replace")


def parse_maps(payload):
    out, seen = [], set()
    for m in re.finditer(r'\{"id":"(\d{9})","city":"([^"]*)","name":"([^"]*)","streetName":"([^"]*)",'
                         r'"region":"([^"]*)","isTown":(true|false),[^{}]*?"monsterTypeCount":(\d+)', payload):
        mid, city, name, street, region, town, mobs = m.groups()
        if mid in seen:
            continue
        seen.add(mid)
        tail = payload[m.end():m.end() + 600]
        disp = re.search(r'"displayRegion":"([^"]*)"', tail)
        out.append({"id": mid, "city": city, "name": name.strip(), "street": street, "region": region,
                    "region_name": disp.group(1) if disp else region, "town": town == "true",
                    "mobs": int(mobs)})
    return out


def parse_item_index(payload):
    out = {}
    for page, name in re.findall(r'"href":"/msclassic/item-db/(\d+)","className":"[^"]*","children":\["([^"]+)"',
                                 payload):
        out.setdefault(name.strip(), int(page))
    return out


def parse_map_monsters(payload):
    m = re.search(r'"monsters":\[(.*?)\]', payload)
    if not m:
        return []
    return [{"id": int(i), "name": n, "count": int(c)}
            for i, n, c in re.findall(r'\{"id":"(\d+)","name":"([^"]+)","count":(\d+)', m.group(1))]


def parse_drops(payload):
    rows = re.findall(r'"href":"/msclassic/item-db/(\d+)"[^\n]{0,600}?icons/(\d{7})\.png[^\n]{0,400}?'
                      r'"children":"([^"]{2,80})"\}\]\]\}\]\}\]?,\["\$","td"[^\n]{0,400}?"children":"([\d.]+%)"',
                      payload)
    out, seen = [], set()
    for page, icon, name, chance in rows:
        if page not in seen:
            seen.add(page)
            out.append({"page": int(page), "id": int(icon), "name": name, "chance": chance})
    return out


def parse_item(payload, html_text):
    info = {}
    name = re.search(r'"itemName":"([^"]+)"', payload)
    if name:
        info["name"] = name.group(1)
    else:
        title = re.search(r"<title>(.*?)(?: - MapleStory| \| NiaMeowDB)", html_text)
        info["name"] = html.unescape(title.group(1)).strip() if title else None
    # the NPC sell-back price: in the page's data (after any stat block), or else in its text
    m = re.search(r'"npcSellBack":\{"price":(\d+)', payload)
    if not m:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text))
        m = re.search(r"Sell-back(?: \(per unit\))? ([\d,]+) mesos", text)
    info["npc"] = int(m.group(1).replace(",", "")) if m else None
    icon = re.search(r'"iconKey":"(\d+)"', payload)
    info["id"] = int(icon.group(1)) if icon else None
    info["equip"] = '"isEquip":true' in payload
    cat = re.search(r'\\?"category\\?":\\?"([^"\\]+)', payload)
    info["category"] = cat.group(1) if cat else ""
    info["untradeable"] = '"children":"Untradeable"' in payload
    return info


def parse_drops_html(html_text):
    """A monster's drops from the page as the browser shows it (pages saved with Ctrl+S)."""
    out, seen = [], set()
    for page, icon, name, chance in re.findall(
            r'href="/msclassic/item-db/(\d+)"><img src="https://cdn\.meowdb\.com/icons/(\d{7})\.png"[^>]*/?>\s*'
            r'<span[^>]*>([^<]+)</span></a></td><td[^>]*>(?:<a[^>]*>)?([\d.]+%)', html_text):
        if page not in seen:
            seen.add(page)
            out.append({"page": int(page), "id": int(icon), "name": html.unescape(name), "chance": chance})
    return out


def parse_classic_drops(html_text):
    """The "Drops (MS Classic)" section of a monster page as the browser shows it: what players
    have seen drop in MapleStory Classic, with their weighted votes (seen it / not). It has no drop
    chances; the reference tables further down (MSEA, osmlib) are other versions' data."""
    i = html_text.find("Drops (MS Classic)")
    if i < 0:
        return []
    ends = [k for k in (html_text.find("MSEA reference drops", i), html_text.find("OSMLIB Reference", i)) if k > 0]
    seg = html_text[i:min(ends) if ends else len(html_text)]
    out, seen = [], set()
    for m in re.finditer(r'href="[^"]*/msclassic/item-db/(\d+)"><img alt="([^"]*)"[^>]*src="[^"]*?(\d{7})[^"]*"', seg):
        page, name, icon = m.groups()
        if page in seen:
            continue
        seen.add(page)
        row = seg[m.end():m.end() + 1500]
        cat = re.search(r'title="[^"]*">[^<]*</div><div[^>]*>([^<]+)</div>', row)
        up = re.search(r'▲</span><span[^>]*>([\d.]+)</span>', row)
        down = re.search(r'▼</span><span[^>]*>([\d.]+)</span>', row)
        out.append({"page": int(page), "id": int(icon), "name": html.unescape(name),
                    "cat": html.unescape(cat.group(1)) if cat else "",
                    "up": float(up.group(1)) if up else 0.0, "down": float(down.group(1)) if down else 0.0,
                    "source": "MS Classic"})
    out.sort(key=lambda d: d["down"] - d["up"])      # most-confirmed drops first
    return out


def parse_monsters_html(html_text):
    out, seen = [], set()
    for mid, name in re.findall(r'href="/msclassic/monsters/(\d+)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,40}?)\s*<', html_text):
        if mid not in seen:
            seen.add(mid)
            out.append({"id": int(mid), "name": html.unescape(name), "count": 0})
    return out


MEOW_IMPORT_DIRS = [os.path.join(HERE, "meowdb_import"),
                    os.path.join(os.path.expanduser("~"), "Downloads", "meowdb")]


def parse_mob_locations(payload):
    """The maps a monster lives in (from its page): [(map id, map name, how many)]."""
    m = re.search(r'"locations":\[(.*?)\]\}', payload)
    if not m:
        return []
    return [(mid, name, int(n)) for mid, name, n in
            re.findall(r'\{"mapId":"(\d{9})","mapName":"([^"]+)","streetName":"[^"]*","count":(\d+)', m.group(1))]


IMPORT_VERSION = 2          # bump when a parser changes, so saved pages are read again


def import_saved_pages(folders=None):
    """Pages you saved from meowdb.com in your own browser (Ctrl+S) -> the local cache, exactly as
    if MapleTracker had looked them up. Nothing is fetched. When a map's own page isn't saved,
    its monsters come from the monster pages (each lists the maps it lives in). Item icons in a
    complete save's "_files" folder are kept too. -> [(kind, id, name or None, info)]"""
    done, map_pages, from_mobs = [], set(), {}
    pages = []
    # pages already read (same size and time) are skipped: re-reading hundreds of MB on every
    # MOBS click made it hang
    seen_path = MeowDB._path("imported_pages")
    seen = load_json(seen_path, {}) or {}
    if seen.get("_version") != IMPORT_VERSION:
        seen = {"_version": IMPORT_VERSION}
    changed = False
    for folder in folders or MEOW_IMPORT_DIRS:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):    # subfolders too (e.g. an "items" folder)
            # a complete save's "_files" folder: take its icons, don't walk into it
            pages += [os.path.join(root, d) for d in sorted(dirs) if d.endswith("_files")]
            dirs[:] = sorted(d for d in dirs if not d.endswith("_files"))
            pages += [os.path.join(root, f) for f in sorted(files)]
    for path in pages:
        if os.path.isdir(path) and path.endswith("_files"):
            for f in os.listdir(path):
                mm = re.fullmatch(r"(\d{7})(?:_[A-Za-z0-9]+)?\.png", f)
                dst = os.path.join(ICON_DIR, f"{mm.group(1)}.png") if mm else None
                if dst and not os.path.exists(dst):
                    os.makedirs(ICON_DIR, exist_ok=True)
                    try:
                        with open(os.path.join(path, f), "rb") as a, open(dst, "wb") as b:
                            b.write(a.read())
                    except OSError:
                        pass
            continue
        if not path.lower().endswith((".html", ".htm")):
            continue
        try:
            st = os.stat(path)
            stamp = [st.st_size, int(st.st_mtime)]
            if seen.get(path) == stamp:
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        seen[path] = stamp
        changed = True
        m = re.search(r'rel="canonical" href="https://meowdb\.com/msclassic/(maps|monsters|item-db)/(\d+)"', text)
        if not m:
            m = re.search(r'meowdb\.com/msclassic/(maps|monsters|item-db)/(\d+)', text)
        if not m:
            continue
        kind, oid = m.groups()
        payload = _next_payload(text)
        title = re.search(r"<title>(.*?)(?: - MapleStory| \| NiaMeowDB)", text)
        name = html.unescape(title.group(1)).strip() if title else None
        if kind == "maps":
            info = parse_map_monsters(payload) or parse_monsters_html(text)
            key = f"map_{oid}"
            map_pages.add(oid)
        elif kind == "monsters":
            # what players saw drop in Classic; the other versions' reference tables only if missing
            info = parse_classic_drops(text) or parse_drops(payload) or parse_drops_html(text)
            key = f"mob_{int(oid)}"
            for mid, _mname, n in parse_mob_locations(payload):
                from_mobs.setdefault(mid, {})[int(oid)] = {"id": int(oid), "name": name, "count": n}
        else:
            info = parse_item(payload, text)
            key = f"item_{int(oid)}"
        if info:
            save_json(MeowDB._path(key), info)
            if kind == "monsters":
                save_json(MeowDB._path(f"mobname_{int(oid)}"), {"name": name})
            done.append((kind, oid, name, info))
    # maps known only through their monsters' pages (a saved map page always wins)
    for mid, mobs in from_mobs.items():
        if mid in map_pages:
            continue
        path = MeowDB._path(f"map_{mid}")
        marker = MeowDB._path(f"mapfrommobs_{mid}")
        if os.path.exists(path) and not os.path.exists(marker):
            continue                          # from the map's own page, earlier
        old = {m["id"]: m for m in (load_json(path, []) or [])} if os.path.exists(marker) else {}
        old.update(mobs)
        save_json(path, sorted(old.values(), key=lambda m: -m.get("count", 0)))
        save_json(marker, {"from": "monster pages"})
    if changed:
        save_json(seen_path, seen)
    return done


def icon_ids():
    """{lower-case item name: icon / item id} from the potion list and everything cached (monster
    drop lists and item pages)."""
    out = {name.lower(): iid for iid, name, *_ in CATALOG}
    try:
        files = os.listdir(MEOW_DIR)
    except OSError:
        return out
    for f in files:
        if f.startswith("mob_") and f.endswith(".json"):
            for dr in load_json(os.path.join(MEOW_DIR, f), []) or []:
                if isinstance(dr, dict) and dr.get("name") and dr.get("id"):
                    out.setdefault(dr["name"].lower(), dr["id"])
        elif f.startswith("item_") and f[5:-5].isdigit():
            info = load_json(os.path.join(MEOW_DIR, f), {}) or {}
            if info.get("name") and info.get("id"):
                out[info["name"].lower()] = info["id"]
    return out


class MeowDB:
    """Polite, cached access to NiaDB. Callers get results through a callback on the Tk thread
    (immediately when cached); a background thread fetches one page at a time, 10 s apart."""

    def __init__(self, app=None, online=False):
        self.app = app
        self.online = online                 # False: only what's already cached, never a request
        os.makedirs(MEOW_DIR, exist_ok=True)
        self.jobs = queue.Queue()
        self.done = queue.Queue()            # (callback, result) for the Tk thread
        self.pending = {}                    # cache key -> [callbacks]
        self.blocked = None                  # why fetching stopped (429 / 403 / errors)
        self.last = 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- cache
    @staticmethod
    def _path(key):
        return os.path.join(MEOW_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def cached(self, key, max_age_days=None):
        p = self._path(key)
        if not os.path.exists(p):
            return None
        if max_age_days is not None and time.time() - os.path.getmtime(p) > max_age_days * 86400:
            return None
        return load_json(p, None)

    # --- requests
    def get(self, key, url_path, parse, callback, max_age_days=None):
        """Look something up: from the cache, or queued for fetching. callback(result or None)."""
        hit = self.cached(key, max_age_days)
        if hit is not None:
            callback(hit)
            return
        if not self.online:
            callback(self.cached(key))        # an old copy, or None - nothing is fetched
            return
        stale = self.cached(key)              # an old copy is fine while a new one is fetched
        with self.lock:
            if key in self.pending:
                self.pending[key].append(callback)
                return
            self.pending[key] = [callback]
        if stale is not None:
            callback(stale)
            with self.lock:
                self.pending[key] = []
        self.jobs.put((key, url_path, parse))

    def maps(self, cb):
        self.get("maps", "/maps", lambda t, p: parse_maps(p), cb, max_age_days=7)

    def item_index(self, cb):
        self.get("item_index", "/item-db/all", lambda t, p: parse_item_index(p), cb, max_age_days=7)

    def map_monsters(self, map_id, cb):
        self.get(f"map_{map_id}", f"/maps/{map_id}", lambda t, p: parse_map_monsters(p), cb)

    def drops(self, mob_id, cb):
        self.get(f"mob_{mob_id}", f"/monsters/{mob_id}", lambda t, p: parse_drops(p), cb)

    def item(self, page, cb):
        self.get(f"item_{page}", f"/item-db/{page}", lambda t, p: parse_item(p, t), cb)

    def queued(self):
        return self.jobs.qsize()

    def _worker(self):
        import urllib.error
        import urllib.request
        while True:
            key, path, parse = self.jobs.get()
            if self.blocked:
                self._finish(key, None)
                continue
            wait = MEOW_GAP_S - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            result = None
            try:
                req = urllib.request.Request(MEOW_BASE + path, headers={"User-Agent": MEOW_UA})
                text = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
                result = parse(text, _next_payload(text))
                save_json(self._path(key), result)
            except urllib.error.HTTPError as ex:
                if ex.code in (403, 429):
                    self.blocked = f"NiaDB answered {ex.code} - stopped asking it for data this run"
                    log(self.blocked)
                else:
                    log(f"NiaDB {path}: HTTP {ex.code}")
            except Exception as ex:
                log(f"NiaDB {path}: {ex!r}")
            self._finish(key, result)

    def _finish(self, key, result):
        with self.lock:
            cbs = self.pending.pop(key, [])
        for cb in cbs:
            self.done.put((cb, result))

    def poll(self):
        """Run finished callbacks (call from the Tk thread)."""
        while not self.done.empty():
            cb, result = self.done.get_nowait()
            try:
                cb(result)
            except Exception as ex:
                log("NiaDB callback:", repr(ex))


# ----------------------------------------------------------------------------- loot: names and prices
def loot_kind(name, cat):
    """Which PROFIT switch an item falls under: etc / equip / scroll / use / setup."""
    if "scroll" in name.lower():
        return "scroll"
    return {"Equip": "equip", "Use": "use", "Setup": "setup"}.get(cat or "", "etc")


class ItemNames:
    """Snaps item names read off the screen to the game's real names (NiaDB's item list, saved in
    meowdb_cache/): exact, then a cut-off name's start ("Animal Fur Bun..."), then OCR slips
    ("Maple Leat" -> "Maple Leaf"). Unknown names are kept as read."""

    def __init__(self, names=None):
        self.set_names(names or [])
        self.memo = {}

    def set_names(self, names):
        self.names = sorted(set(names))
        self.lower = {n.lower(): n for n in self.names}
        self.memo = {}

    def canon(self, raw):
        if raw in self.memo:
            return self.memo[raw]
        out = self._canon(raw)
        self.memo[raw] = out
        return out

    def _canon(self, raw):
        import difflib
        if not self.names:
            return raw
        cut = raw.endswith("...")
        base = raw[:-3].strip() if cut else raw
        if base.lower() in self.lower:
            return self.lower[base.lower()]
        if cut and len(base) >= 6:
            starts = [n for n in self.names if n.lower().startswith(base.lower())]
            if len(starts) == 1:
                return starts[0]
            if starts:
                return raw                    # several items start like this: keep it as read
        if cut and len(base) >= 6:
            # "nimal Fur Bun..." / "P.nimal Fur Bun...": the start of a real name, read loosely
            bl = base.lower()
            sc = sorted(((max(difflib.SequenceMatcher(None, bl, n.lower()[:len(bl) + j]).ratio()
                              for j in (-1, 0, 1)), n) for n in self.names
                         if abs(len(n) - len(bl)) < 40 and n.lower()[len(bl) // 2:].find(bl[len(bl) // 2:len(bl) // 2 + 3]) >= -1),
                        reverse=True)[:2]
            if sc and sc[0][0] >= 0.8 and (len(sc) == 1 or sc[0][0] > sc[1][0] + 0.04):
                return sc[0][1]
        close = difflib.get_close_matches(base, self.names, n=2, cutoff=0.84)
        if not close and len(base) >= 5:
            # a letter or two cut off the front ("ig's Ribbon", "eather", "titt Feather")
            ends = [n for n in self.names if len(n) - 3 <= len(base) <= len(n)]
            close = difflib.get_close_matches(base.lower(), [n.lower()[len(n) - len(base):] for n in ends], n=2,
                                              cutoff=0.8)
            close = [next(n for n in ends if n.lower()[len(n) - len(base):] == c) for c in close]
        if close and (len(close) == 1 or difflib.SequenceMatcher(None, base, close[0]).ratio() >
                      difflib.SequenceMatcher(None, base, close[1]).ratio() + 0.03):
            return close[0]
        return raw


class PriceBook:
    """What each item is worth to you, shared by all characters (prices.json): your own price if
    you set one, else the NPC sell-back price when known (from NiaDB), else 0 - scrolls default
    to 1 meso. Each item can also be left out of PROFIT."""

    def __init__(self, path=PRICES_PATH):
        self.path = path
        self.items = load_json(path, {})

    def save(self):
        try:
            save_json(self.path, self.items)
        except OSError as ex:
            log("could not save prices:", ex)

    def entry(self, name, cat=None):
        e = self.items.get(name)
        if e is None:
            e = self.items[name] = {"price": None, "npc": None, "cat": cat, "include": True,
                                    "seen": time.time()}
        elif cat and not e.get("cat"):
            e["cat"] = cat
        return e

    def seen(self, name, cat):
        e = self.entry(name, cat)
        e["seen"] = time.time()
        return e

    def unit(self, name):
        """(price per item, where it came from: "you" / "NPC" / "default" / None)."""
        e = self.items.get(name) or {}
        if e.get("price") is not None:
            return e["price"], "you"
        if e.get("npc") is not None:
            return e["npc"], "NPC"
        if loot_kind(name, e.get("cat")) == "scroll":
            return 1, "default"
        return 0, None

    def set_price(self, name, price):
        self.entry(name)["price"] = price
        self.save()

    def set_include(self, name, on):
        self.entry(name)["include"] = bool(on)
        self.save()


# ----------------------------------------------------------------------------- sessions / profiles
STOPPED, RUNNING, PAUSED = "stopped", "running", "paused"
MIN_LOG_S = 60                 # shorter sessions aren't worth a log entry
SEGMENT_S = 3600               # a named session is logged after every hour of tracking


class Session:
    """One tracking session. Only time spent running counts, and only gains made while running
    are added, so pausing freezes both the numbers and the clock."""

    def __init__(self, map_name=""):
        self.state = STOPPED
        self.map = map_name
        self.clear()

    def clear(self):
        self.started = None        # wall time this session (or hourly segment) began
        self.active = 0.0          # seconds spent running
        self.exp = self.levels = 0
        self.exp_t = 0.0           # seconds the EXP numbers cover (Reset EXP restarts it)
        self.meso = self.pickups = 0
        self.meso_t = 0.0
        self.pots = {}             # potion name -> {"id", "used", "cost"}
        self.pot_t = 0.0
        self.loot = {}             # item name -> {"qty", "cat"}
        self.loot_t = 0.0

    @property
    def pot_cost(self):
        return sum(p["cost"] for p in self.pots.values())

    def reset_potions(self):
        self.pots, self.pot_t = {}, 0.0

    def reset_loot(self):
        self.loot, self.loot_t = {}, 0.0

    def add_loot(self, name, qty, cat):
        if self.running:
            e = self.loot.setdefault(name, {"qty": 0, "cat": cat})
            e["qty"] += qty
            e["cat"] = e.get("cat") or cat

    def reset_exp(self):
        self.exp, self.levels, self.exp_t = 0, 0, 0.0

    def reset_meso(self):
        self.meso, self.pickups, self.meso_t = 0, 0, 0.0

    @property
    def running(self):
        return self.state == RUNNING

    def advance(self, dt):
        if self.running:
            self.active += dt
            self.exp_t += dt
            self.meso_t += dt
            self.pot_t += dt
            self.loot_t += dt

    def add_exp(self, gain, levels):
        if self.running:
            self.exp += gain
            self.levels += levels

    def add_meso(self, amt):
        if self.running:
            self.meso += amt
            self.pickups += 1

    def add_potions(self, name, item_id, n, price):
        if self.running:
            p = self.pots.setdefault(name, {"id": item_id, "used": 0, "cost": 0})
            p["used"] += n
            p["cost"] += n * (price or 0)

    def loggable(self):
        return bool(self.map.strip()) and self.started is not None and self.active >= MIN_LOG_S

    def log_entry(self, reason, need=None, trackers=None, loot=None):
        """loot: (value, [item rows]) from the app's price book, or None."""
        on = trackers or {"exp": True, "meso": True, "potions": True}
        exp_h, meso_h = per_hour(self.exp, self.exp_t), per_hour(self.meso, self.meso_t)
        e = {"id": uuid.uuid4().hex[:12], "map": self.map.strip(), "start": self.started,
             "end": time.time(), "active_s": int(round(self.active)), "reason": reason}
        if on.get("exp"):
            e.update(exp=self.exp, exp_h=exp_h, levels=self.levels,
                     lvl_pct_h=(exp_h / need * 100) if (exp_h and need) else None)
        if on.get("meso"):
            e.update(meso=self.meso, meso_h=meso_h, pickups=self.pickups)
        if on.get("potions"):
            e.update(potions={k: dict(v) for k, v in self.pots.items()}, pot_cost=self.pot_cost,
                     pot_h=per_hour(self.pot_cost, self.pot_t))
        if on.get("loot") and loot is not None:
            e.update(loot_value=loot[0], loot=loot[1], loot_h=per_hour(loot[0], self.loot_t))
        if sum(bool(on.get(k)) for k in ("meso", "loot", "potions")) >= 2:
            e["profit"] = (self.meso if on.get("meso") else 0) + (e.get("loot_value", 0)) - \
                (self.pot_cost if on.get("potions") else 0)
            e["profit_h"] = per_hour(e["profit"], self.active)
        return e

    FIELDS = ("state", "map", "started", "active", "exp", "levels", "exp_t", "meso", "pickups", "meso_t",
              "pots", "pot_t", "loot", "loot_t")

    def to_json(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def from_json(cls, d):
        s = cls()
        for f in cls.FIELDS:
            if f in d:
                setattr(s, f, d[f])
        return s


class Profiles:
    """One file per character in profiles/, holding that character's logs."""

    def __init__(self, folder=PROFILES_DIR):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    @staticmethod
    def clean(name):
        """A character name that's safe as a file name."""
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip(" .")[:24]

    def _path(self, name):
        return os.path.join(self.folder, self.clean(name) + ".json")

    def names(self):
        out = [f[:-5] for f in os.listdir(self.folder) if f.lower().endswith(".json")]
        return sorted(out, key=str.lower)

    def exists(self, name):
        return self.clean(name).lower() in (n.lower() for n in self.names())

    def load(self, name):
        data = load_json(self._path(name), {})
        data["name"] = self.clean(name)
        data.setdefault("logs", [])
        data.setdefault("potions", [])     # [{"id", "name", "price", "slot": [col, row]}]
        return data

    def save(self, data):
        save_json(self._path(data["name"]), {"name": data["name"], "logs": data["logs"],
                                             "potions": data.get("potions", [])})

    def create(self, name):
        self.save({"name": self.clean(name), "logs": [], "potions": []})

    def rename(self, old, new):
        data = self.load(old)
        data["name"] = self.clean(new)
        # move the old file aside first, so a change of case only ("maple" -> "Maple") works
        # on Windows, where both names are the same file
        tmp = self._path(old) + ".renaming"
        os.replace(self._path(old), tmp)
        self.save(data)
        os.remove(tmp)

    def delete(self, name):
        try:
            os.remove(self._path(name))
        except OSError:
            pass


# ----------------------------------------------------------------------------- formatting
def parse_mesos(text):
    """A price typed in: "13000", "13,000", "13k", "1.55m", "2b" -> mesos, or None if it isn't one."""
    s = (text or "").lower().replace(",", "").replace(" ", "").replace("mesos", "").replace("meso", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)([kmb]?)", s)
    if not m:
        return None
    return int(round(float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2)]))


def fmt_int(n):
    return f"{int(n):,}"


def fmt_short(n):
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div * 10:
            return f"{n / div:,.1f}{suf}" if n < div * 100 else f"{n / div:,.0f}{suf}"
    return f"{n:,.0f}"


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec % 60:02d}s"


def per_hour(gained, seconds):
    return gained * 3600.0 / seconds if seconds >= 30 else None


# ----------------------------------------------------------------------------- UI drawing
# Everything is drawn with PIL in the look of the game's own UI (colours sampled from the status
# bar, the minimap window, the Quest Helper and the glossy menu buttons) - original drawing, no
# game art. Windows use a colour key, so whatever isn't drawn is see-through.
KEY = "#010203"
KEY_RGB = (1, 2, 3)
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_FONTS = {}
FONT_FILES = {"black": "ariblk.ttf", "bold": "arialbd.ttf", "tahoma": "tahomabd.ttf", "reg": "tahoma.ttf"}

WHITE, BLACK = (255, 255, 255), (0, 0, 0)
BRACKET_GREEN = (178, 226, 0)        # the [ ] around the status bar's numbers
MESO_GOLD = (255, 214, 64)
POT_RED = (255, 150, 130)
PROFIT_GREEN, PROFIT_RED = (120, 236, 80), (255, 86, 76)
DIM_TEXT = (196, 214, 245)
# window chrome (minimap window / Quest Helper)
FRAME_OUT, FRAME_FILL, FRAME_LIGHT = (111, 134, 160), (224, 236, 248), (255, 255, 255)
BODY_FILL, TITLE_EDGE, NAVY = (153, 187, 204), (114, 131, 173), (22, 45, 90)
PANEL_TOP, PANEL_BOT, PANEL_EDGE = (98, 132, 210), (126, 160, 226), (170, 196, 240)
# glossy buttons: (top, bottom, border, text outline)
BTN_BLUE = ((22, 128, 214), (0, 78, 145), (0, 44, 92), (0, 40, 86))
BTN_ORANGE = ((255, 220, 80), (246, 160, 10), (160, 90, 0), (140, 70, 0))
BTN_RED = ((232, 40, 64), (170, 0, 32), (100, 0, 16), (96, 0, 16))


def font(kind, size):
    key = (kind, size)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES[kind]), size)
        except OSError:
            try:
                _FONTS[key] = ImageFont.truetype(os.path.join(FONT_DIR, "arialbd.ttf"), size)
            except OSError:
                _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def text_runs(d, x, y, runs, stroke, stroke_fill=BLACK):
    """Draw [(text, font, colour)] one after another on a shared baseline; returns the end x."""
    for text, f, col in runs:
        d.text((x, y), text, font=f, fill=col, anchor="ls", stroke_width=stroke, stroke_fill=stroke_fill)
        x += d.textlength(text, font=f)
    return x


def runs_width(d, runs):
    return sum(d.textlength(t, font=f) for t, f, _ in runs)


def shade(c, k):
    """Lighten (k > 0) or darken (k < 0) a colour."""
    if k >= 0:
        return tuple(int(v + (255 - v) * k) for v in c)
    return tuple(int(v * (1 + k)) for v in c)


def grad_rrect(img, box, r, top, bottom, outline=None):
    """Rounded rectangle with a top-to-bottom gradient."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    t = np.linspace(0.0, 1.0, h)[:, None, None]
    g = (np.array(top)[None, None, :] * (1 - t) + np.array(bottom)[None, None, :] * t)
    g = np.repeat(g, w, axis=1).astype(np.uint8)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    img.paste(Image.fromarray(g, "RGB"), (x0, y0), mask)
    if outline:
        ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=r, outline=outline)


def glossy_button(img, box, pal, text, k, state=None):
    """A glossy game-style button. state: None, 'hover' or 'down'."""
    top, bot, border, tstroke = pal
    if state == "hover":
        top, bot = shade(top, 0.18), shade(bot, 0.15)
    elif state == "down":
        top, bot = shade(bot, -0.1), shade(top, -0.05)
    x0, y0, x1, y1 = box
    r = max(2, int(3 * k))
    grad_rrect(img, box, r, top, bot, outline=border)
    # the shine on the upper half (its own layer: drawing translucent colour straight onto an
    # RGBA image would replace the alpha and punch a hole)
    shine = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shine).rounded_rectangle([x0 + 2, y0 + 2, x1 - 3, y0 + (y1 - y0) * 0.45],
                                            radius=max(1, r - 1), fill=(255, 255, 255, 60))
    img.alpha_composite(shine)
    d = ImageDraw.Draw(img)
    f = font("black", max(8, int(round(7.6 * k))))
    tw = d.textlength(text, font=f)
    dy = 1 if state == "down" else 0
    d.text(((x0 + x1 - tw) / 2, (y0 + y1) / 2 + 3.2 * k + dy), text, font=f, fill=WHITE, anchor="ls",
           stroke_width=max(1, int(round(1.1 * k))), stroke_fill=tstroke)


def exp_gauge(d, x, y, w, h, frac):
    """The status bar's EXP gauge: black edge, cream frame, grey trough, yellow-green fill."""
    frac = max(0.0, min(1.0, frac))
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=BLACK)
    d.rectangle([x + 1, y + 1, x + w - 2, y + h - 2], fill=(255, 255, 218))
    d.rectangle([x + 2, y + 2, x + w - 3, y + h - 3], fill=(120, 120, 120))
    fw = int(round((w - 4) * frac))
    if fw <= 0:
        return
    stops = [(0.0, (232, 250, 90)), (0.25, (210, 232, 0)), (0.6, (155, 172, 0)), (1.0, (108, 123, 0))]
    inner = h - 4
    for i in range(inner):
        t = i / max(1, inner - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t <= t1:
                q = (t - t0) / (t1 - t0)
                c = tuple(int(c0[j] + (c1[j] - c0[j]) * q) for j in range(3))
                break
        d.line([x + 2, y + 2 + i, x + 1 + fw, y + 2 + i], fill=c)


def meso_coin(d, cx, cy, r):
    """A small gold coin."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(150, 92, 0))
    d.ellipse([cx - r + 1, cy - r + 1, cx + r - 1, cy + r - 1], fill=(250, 196, 30))
    ri = r * 0.62
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], outline=(196, 128, 0), width=max(1, int(r / 5)))
    d.ellipse([cx - r * 0.55, cy - r * 0.7, cx - r * 0.05, cy - r * 0.25], fill=(255, 244, 170))


def to_keyed(img_rgba):
    """Flatten onto the colour key (anti-aliased edges blend toward black, which the outlines hide)."""
    base = Image.new("RGB", img_rgba.size, KEY_RGB)
    base.paste(img_rgba, (0, 0), img_rgba)
    return base


# ----------------------------------------------------------------------------- overlay
class Overlay:
    """The always-on-top readout: outlined text and an EXP gauge straight over the game, no
    background. Drag the text to move it; right-click it for the tracker window."""

    def __init__(self, app):
        self.app = app
        cfg = app.cfg
        self.k = cfg.get("overlay_scale") or max(1.0, app.root.winfo_screenheight() / 768)
        w = self.win = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=KEY)
        w.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(w, bg=KEY, highlightthickness=0, cursor="fleur")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", lambda e: app.open_controls())
        self.canvas.bind("<Motion>", self._motion)
        self._photo, self._last = None, None
        w.update_idletasks()
        self.hwnd = top_hwnd(w)
        exclude_from_capture(self.hwnd, app.cfg.get("hide_from_screenshots"))
        self._placed = False
        self._grab = None
        # the underlay: a rounded game-style panel in its own see-through window just behind the
        # text (the text window's colour key can't do part-transparency)
        u = self.under = tk.Toplevel(app.root)
        u.overrideredirect(True)
        u.attributes("-topmost", True)
        u.configure(bg=KEY)
        u.attributes("-transparentcolor", KEY)
        self.ucanvas = tk.Canvas(u, bg=KEY, highlightthickness=0, cursor="fleur")
        self.ucanvas.pack()
        self.ucanvas.bind("<ButtonPress-1>", self._press)
        self.ucanvas.bind("<B1-Motion>", self._drag)
        self.ucanvas.bind("<ButtonRelease-1>", self._release)
        self.ucanvas.bind("<Button-3>", lambda e: app.open_controls())
        u.withdraw()
        u.update_idletasks()
        self.uhwnd = top_hwnd(u)
        exclude_from_capture(self.uhwnd, app.cfg.get("hide_from_screenshots"))
        self._uphoto, self._ubox, self._ushown, self._ukey = None, None, False, None
        self.place()

    def place(self, hwnd=None):
        cfg = self.app.cfg
        if cfg["panel_x"] is not None:
            self.win.geometry(f"+{cfg['panel_x']}+{cfg['panel_y']}")
            self._placed = True
        elif hwnd:
            x, y, w, h = client_rect(hwnd)
            self.win.geometry(f"+{x + int(w * 0.01)}+{y + int(h * 0.42)}")
            self._placed = True
        self._place_under()

    UNDER_STYLE = {   # fill, outer edge, inner highlight: like the game's windows
        "dark": ((18, 26, 40), (6, 10, 18), (84, 104, 136)),
        "light": ((232, 238, 246), (92, 108, 136), (255, 255, 255)),
    }

    @staticmethod
    def draw_under(size, style, k):
        w, h = size
        fill, edge, hi = Overlay.UNDER_STYLE.get(style, Overlay.UNDER_STYLE["dark"])
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = int(7 * k)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=edge)
        d.rounded_rectangle([1, 1, w - 2, h - 2], radius=r - 1, fill=hi)
        d.rounded_rectangle([2, 2, w - 3, h - 3], radius=max(1, r - 2), fill=fill)
        return img

    def _place_under(self):
        cfg = self.app.cfg
        style = cfg.get("underlay", "off")
        if style == "off" or self._ubox is None or not self.win.winfo_viewable():
            if self._ushown:
                self.under.withdraw()
                self._ushown = False
            return
        bx0, by0, bw, bh = self._ubox
        self.under.geometry(f"{bw}x{bh}+{self.win.winfo_x() + bx0}+{self.win.winfo_y() + by0}")
        self.under.attributes("-alpha", max(0.02, min(1.0, float(cfg.get("underlay_opacity", 0.55)))))
        if not self._ushown:
            self.under.deiconify()
            self.under.attributes("-topmost", True)
            self._ushown = True
        self.win.lift()

    loot_row = None
    btn_boxes = {}

    def _press(self, e):
        self._grab = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        self._down = (e.x_root, e.y_root, e.y_root - self.win.winfo_rooty())   # also from the underlay

    def _motion(self, e):
        on = next((n for n, (x0, y0, x1, y1) in Overlay.btn_boxes.items()
                   if x0 - 2 <= e.x <= x1 + 2 and y0 - 2 <= e.y <= y1 + 2), None)
        self.canvas.config(cursor="hand2" if on else "fleur")
        if on != getattr(self, "hover_btn", None):
            self.hover_btn = on
            self.app.render(time.time())

    def _drag(self, e):
        if self._grab:
            self.win.geometry(f"+{e.x_root - self._grab[0]}+{e.y_root - self._grab[1]}")
            self.win.update_idletasks()
            self._place_under()

    def _release(self, e):
        down = getattr(self, "_down", None)
        if self._grab and down and abs(e.x_root - down[0]) + abs(e.y_root - down[1]) < 4:
            self._grab = None                # a click, not a drag
            ex, ey = down[0] - self.win.winfo_rootx(), down[2]
            for name, (bx0, by0, bx1, by1) in Overlay.btn_boxes.items():
                if bx0 - 2 <= ex <= bx1 + 2 and by0 - 2 <= ey <= by1 + 2:
                    app = self.app
                    if name == "gear":
                        app.open_controls()
                    elif name == "toggle":
                        app.toggle()
                    elif name == "stop":
                        app.stop()
                    app.render(time.time())
                    return
            r = Overlay.loot_row
            if r and r[0] <= down[2] < r[1] and self.app.cfg["trackers"].get("loot"):
                self.app.cfg["overlay_loot_open"] = not self.app.cfg.get("overlay_loot_open")
                save_json(CONFIG_PATH, self.app.cfg)
                self.app.render(time.time())
            return
        if self._grab:
            self._grab = None
            self.app.cfg["panel_x"], self.app.cfg["panel_y"] = self.win.winfo_x(), self.win.winfo_y()
            save_json(CONFIG_PATH, self.app.cfg)

    def set_visible(self, on):
        if on and not self.win.winfo_viewable():
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            self.win.update_idletasks()
            self._place_under()
        elif not on and self.win.winfo_viewable():
            self.win.withdraw()
            self._place_under()

    @staticmethod
    def draw(s, k):
        """The overlay as an RGBA image: a session line, then one block per tracker that's on."""
        on = s.get("on") or {"exp": True, "meso": True, "potions": True}
        W = int(250 * k)
        Overlay.loot_row = None
        rows = -(-len(s.get("loot_grid") or ()) // 4) if s.get("loot_open") else 0
        img = Image.new("RGBA", (W, int((230 + 19 * rows) * k)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        st = max(1, int(round(1.25 * k)))
        lab, num, small = font("black", int(round(11 * k))), font("bold", int(round(10.5 * k))),             font("bold", int(round(8.6 * k)))
        x0 = int(3 * k)
        P = lambda v: int(round(v * k))                # game units -> px
        # > Kerning Subway <Line 1> · 12m   |  || PAUSED · map   |  [] STOPPED · press play to start
        state = s.get("state", STOPPED)
        isz = P(7.5)
        state_icon(d, x0 + 1, P(3), isz, state, STATE_COL[state])
        where = s.get("map") or "(no map name)"
        clock = f"  ·  {s['elapsed']}" if s.get("elapsed") else ""
        label = {RUNNING: where + clock, PAUSED: f"PAUSED  ·  {where}{clock}",
                 STOPPED: "STOPPED  ·  press play to start"}[state]
        colour = WHITE if state == RUNNING else shade(STATE_COL[state], 0.35)
        label = fit_text(d, label, small, W - isz - 12 * k - P(48))
        label_end = text_runs(d, x0 + isz + 5 * k, P(10.5), [(label, small, colour)], st)
        y = 14                                          # game-unit cursor
        if on.get("exp"):
            # EXP.  2,675 [45.2K/h]  +  gauge with level % and time to level
            x = text_runs(d, x0, P(y + 14), [("EXP.", lab, WHITE)], st) + 5 * k
            mode = s.get("exp_display", "exp")
            if mode == "pct":
                runs = [(s.get("exppct", "-%"), num, WHITE), (" [", num, BRACKET_GREEN),
                        (s.get("pcth", "-%/h"), num, WHITE), ("]", num, BRACKET_GREEN)]
            else:
                runs = [(s["exp"], num, WHITE), (" [", num, BRACKET_GREEN), (s["exph"], num, WHITE),
                        ("]", num, BRACKET_GREEN)]
                if mode == "both":
                    runs += [(" [", num, BRACKET_GREEN), (s.get("pcth", "-%/h"), num, WHITE),
                             ("]", num, BRACKET_GREEN)]
            text_runs(d, x, P(y + 14), runs, st)
            gy, gh, gw = P(y + 19), max(6, P(7.5)), P(112)
            exp_gauge(d, x0, gy, gw, gh, (s["pct"] or 0) / 100.0)
            info = (f"{s['pct']:.2f}%" if s["pct"] is not None else "--.--%") +                 (f"  LV UP {s['ttl']}" if s["ttl"] else "")
            text_runs(d, x0 + gw + 5 * k, gy + gh - 0.5 * k, [(info, small, WHITE)], st)
            y += 32
        if on.get("meso"):
            # (coin) MESO  1,317 [9.8K/h]
            r = P(5.2)
            meso_coin(d, x0 + r, P(y + 14) - 4 * k, r)
            x = text_runs(d, x0 + 2 * r + 4 * k, P(y + 14), [("MESO", lab, WHITE)], st) + 5 * k
            text_runs(d, x, P(y + 14), [(s["meso"], num, WHITE), (" [", num, MESO_GOLD),
                                        (s["mesoh"], num, WHITE), ("]", num, MESO_GOLD)], st)
            y += 19
        if on.get("potions"):
            # (icons) x12 x5   -1,240 [2.4K/h]
            x = x0
            if not s.get("pots"):
                text_runs(d, x0, P(y + 12), [("POTIONS", small, WHITE), (
                    "  set them up on the POTIONS tab" if not s.get("pots_setup") else "  none used yet",
                    small, DIM_TEXT)], st)
            for item_id, name, used in s.get("pots", [])[:4]:
                ic = item_icon(item_id)
                if ic is not None:
                    sz = P(13)
                    img.alpha_composite(ic.resize((sz, sz), Image.LANCZOS), (int(x), P(y + 1.5)))
                    x += sz + 1
                else:
                    x = text_runs(d, x, P(y + 12), [(fit_text(d, name, small, P(50)), small, WHITE)], st) + 2 * k
                x = text_runs(d, x, P(y + 12), [(f"x{used}", small, WHITE)], st) + 6 * k
            if s.get("pots"):
                text_runs(d, x + 2 * k, P(y + 12), [(f"-{s['potcost']}", num, POT_RED), (" [", num, POT_RED),
                                                     (s["poth"], num, WHITE), ("]", num, POT_RED)], st)
            y += 17
        if on.get("loot"):
            grid = s.get("loot_grid") or ()
            opened = s.get("loot_open")
            row0 = P(y)
            # a small outlined arrow: right = closed, down = open (click the line)
            ax, ay, a = x0 + P(1), P(y + 8.5), P(3.6)
            tri = [(ax, ay - a), (ax + a * 1.6, ay), (ax, ay + a)] if not opened else                 [(ax - a * 0.2, ay - a * 0.8), (ax + a * 1.8, ay - a * 0.8), (ax + a * 0.8, ay + a * 0.9)]
            d.polygon(tri, fill=WHITE, outline=(0, 0, 0))
            x = text_runs(d, x0 + P(9), P(y + 13), [("LOOT", lab, WHITE)], st) + 5 * k
            x = text_runs(d, x, P(y + 13), [(s.get("loot", "0"), num, PROFIT_GREEN), (" [", num, PROFIT_GREEN),
                                            (s.get("looth", "-/h"), num, WHITE), ("]", num, PROFIT_GREEN)], st)
            text_runs(d, x + 6 * k, P(y + 13), [(s.get("lootx", ""), small, DIM_TEXT)], st)
            y += 17
            Overlay.loot_row = (row0, P(y))           # click here to open / close the grid
            if opened:
                # each item picked up this session: icon and how many, most first
                cell, gx = P(30), x0
                if not grid:
                    text_runs(d, x0 + 2 * k, P(y + 11), [("nothing picked up yet", small, DIM_TEXT)], st)
                    y += 15
                for i, (iid, name, qty) in enumerate(grid):
                    if i and i % 4 == 0:      # 4 to a row, then the next row underneath
                        gx, y = x0, y + 19
                    ic = item_icon(iid)
                    if ic is not None:
                        sz = P(15)
                        img.alpha_composite(ic.resize((sz, sz), Image.LANCZOS), (int(gx), P(y + 1)))
                    else:                      # no icon saved
                        d.rounded_rectangle([gx + P(1), P(y + 2), gx + P(14), P(y + 15)], radius=P(2),
                                            fill=(70, 80, 100), outline=(0, 0, 0))
                        text_runs(d, gx + P(4.5), P(y + 12), [("?", small, WHITE)], st)
                    text_runs(d, gx + P(14), P(y + 17), [(fmt_short(qty) if qty >= 1000 else str(qty), small, WHITE)],
                              st)
                    gx += cell
                    if i == len(grid) - 1:
                        y += 20
                Overlay.loot_row = (row0, P(y))
        if sum(bool(on.get(kk)) for kk in ("meso", "loot", "potions")) >= 2:
            col = PROFIT_GREEN if s.get("profit_v", 0) >= 0 else PROFIT_RED
            x = text_runs(d, x0, P(y + 13), [("PROFIT", lab, WHITE)], st) + 5 * k
            text_runs(d, x, P(y + 13), [(s["profit"], num, col), (" [", num, col), (s["profith"], num, WHITE),
                                        ("]", num, col)], st)
            y += 17
        if not any(on.values()):
            text_runs(d, x0, P(y + 12), [("All trackers are off (SETTINGS tab)", small, DIM_TEXT)], st)
            y += 15
        if s.get("status"):
            text_runs(d, x0, P(y + 12), [(s["status"].splitlines()[0], small, (255, 120, 100))], st)
            y += 15
        # the buttons, top right: gear (tracker window), play / pause, stop
        bs, gap = P(14), P(2.5)
        right = max((img.getbbox() or (0, 0, W, 0))[2], label_end + 3 * bs + 2 * gap + P(8))
        right = min(right, W - 2)
        running = state == RUNNING
        specs = (("gear", "gear", "gear"),
                 ("toggle", "pause" if running else "play", "pause" if running else "play"),
                 ("stop", "stop" if state != STOPPED else "off", "stop"))
        Overlay.btn_boxes = {}
        hover = s.get("hover_btn")
        for i, (name, kind, glyph) in enumerate(specs):
            bx1 = right - (2 - i) * (bs + gap)
            box = (int(bx1 - bs), 0, int(bx1), int(bs))
            overlay_button(img, box, kind, glyph, hover == name)
            Overlay.btn_boxes[name] = box
        return img.crop((0, 0, W, P(y + 3)))

    def render(self, s):
        sig = tuple(sorted((k, str(v)) for k, v in s.items() if not k.startswith("c_")))
        if sig == self._last:
            return
        self._last = sig
        img = self.draw(s, self.k)
        self._photo = ImageTk.PhotoImage(to_keyed(img), master=self.canvas)
        self.canvas.config(width=img.width, height=img.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        # the underlay fits what's drawn, with a margin
        style = s.get("under", ("off",))[0]
        box = img.getbbox()
        if style != "off" and box:
            pad = int(6 * self.k)
            x0, y0, x1, y1 = box
            bw, bh = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
            if self._ukey != (bw, bh, style):
                self._uphoto = ImageTk.PhotoImage(to_keyed(self.draw_under((bw, bh), style, self.k)),
                                                  master=self.ucanvas)
                self.ucanvas.config(width=bw, height=bh)
                self.ucanvas.delete("all")
                self.ucanvas.create_image(0, 0, image=self._uphoto, anchor="nw")
                self._ukey = (bw, bh, style)
            self._ubox = (x0 - pad, y0 - pad, bw, bh)
        else:
            self._ubox = None
        self._place_under()


# ----------------------------------------------------------------------------- game-style windows
BTN_GREEN = ((128, 214, 64), (46, 142, 22), (22, 84, 8), (20, 72, 8))
BTN_GREY = ((206, 214, 226), (150, 160, 176), (92, 102, 120), (70, 78, 96))
BTN_OFF = ((190, 198, 210), (165, 174, 188), (130, 140, 154), (128, 136, 150))   # disabled
STATE_COL = {RUNNING: (64, 184, 44), PAUSED: (246, 156, 20), STOPPED: (140, 150, 166)}
STATE_TEXT = {RUNNING: "RUNNING", PAUSED: "PAUSED", STOPPED: "STOPPED"}
INK = (29, 43, 69)              # dark text on light backgrounds
LIST_BG, LIST_ALT = (246, 250, 254), (232, 240, 250)


def state_icon(d, x, y, size, state, fill):
    """Play / pause / stop symbol in a size x size box at (x, y)."""
    if state == RUNNING:
        d.polygon([(x + size * 0.12, y), (x + size * 0.12, y + size), (x + size * 0.95, y + size / 2)],
                  fill=fill, outline=BLACK)
    elif state == PAUSED:
        w = size * 0.32
        d.rectangle([x + size * 0.08, y, x + size * 0.08 + w, y + size], fill=fill, outline=BLACK)
        d.rectangle([x + size * 0.6, y, x + size * 0.6 + w, y + size], fill=fill, outline=BLACK)
    else:
        d.rectangle([x + size * 0.1, y + size * 0.1, x + size * 0.9, y + size * 0.9], fill=fill, outline=BLACK)


def fit_text(d, text, f, width):
    """Cut text with '...' so it fits in width pixels."""
    if d.textlength(text, font=f) <= width:
        return text
    while text and d.textlength(text + "...", font=f) > width:
        text = text[:-1]
    return text + "..."


def wrap_text(d, text, f, width):
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if d.textlength(trial, font=f) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


class MapleWindow:
    """A borderless window drawn like the game's own: pale blue frame, white title strip, glossy
    buttons. Subclasses draw the inside in draw_body() and register clickable areas there."""
    W, H = 300, 120
    TITLE = "MAPLE TRACKER"

    def __init__(self, app):
        self.app = app
        self.k = app.cfg.get("window_scale") or max(1.0, app.root.winfo_screenheight() / 768 * 0.9)
        w = self.win = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=KEY)
        w.attributes("-transparentcolor", KEY)
        self.Wp, self.Hp = self.u(self.W), self.u(self.H)
        self.canvas = tk.Canvas(w, bg=KEY, highlightthickness=0, width=self.Wp, height=self.Hp)
        self.canvas.pack()
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        w.bind("<Escape>", lambda e: self.close())
        self.buttons = {}            # name -> (box, action)
        self._hover_drag = None
        self.drags = {}              # name -> (box, payload): things that can be dragged
        self.drops = {}              # name -> (box, handler(payload)): where they can be dropped
        self.sliders = {}            # name -> (track box, set(0..1), done())
        self.sliding = None
        self.dragging = None         # [payload, start x, start y, mouse x, mouse y]
        self.hover = self.down = None
        self._grab = None
        self._photo = None
        self.closed = False
        w.update_idletasks()
        self.hwnd = top_hwnd(w)
        exclude_from_capture(self.hwnd, app.cfg.get("hide_from_screenshots"))

    def u(self, v):
        return int(round(v * self.k))

    def centre_on(self, other=None):
        if other is not None:
            x = other.winfo_rootx() + (other.winfo_width() - self.Wp) // 2
            y = other.winfo_rooty() + (other.winfo_height() - self.Hp) // 2
        else:
            x = (self.win.winfo_screenwidth() - self.Wp) // 2
            y = (self.win.winfo_screenheight() - self.Hp) // 2
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def entry(self, box, value="", on_change=None, max_len=60):
        """A text box styled like the game's white input fields."""
        x0, y0, x1, y1 = box
        var = tk.StringVar(value=value)
        e = tk.Entry(self.win, textvariable=var, font=("Arial", -self.u(9.6), "bold"), bg="#ffffff",
                     fg="#1d2b45", relief="flat", highlightthickness=1, highlightbackground="#162d5a",
                     highlightcolor="#2f6fd6", insertbackground="#1d2b45", selectbackground="#5f86d0")
        e.bind("<Button-1>", lambda ev: (self.win.focus_force(), e.focus_set()))

        def changed(*_):
            if len(var.get()) > max_len:
                var.set(var.get()[:max_len])
            if on_change:
                on_change(var.get())
        var.trace_add("write", changed)
        item = self.canvas.create_window(x0, y0, window=e, anchor="nw", width=x1 - x0, height=y1 - y0)
        return e, var, item

    # --- input
    def _hit(self, e):
        for name, (box, _) in self.buttons.items():
            x0, y0, x1, y1 = box
            if x0 <= e.x < x1 and y0 <= e.y < y1:
                return name
        return None

    def _set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.canvas.config(cursor="hand2" if name else "")
            self.redraw()

    def _motion(self, e):
        if not self._grab:
            name = self._hit(e)
            drag = None if name else self._in(self.drags, e.x, e.y)
            if drag != self._hover_drag:
                self._hover_drag = drag
                self.redraw()
            self._set_hover(name)
            if drag and not name:
                self.canvas.config(cursor="fleur")

    def _in(self, table, x, y):
        for name, (box, _) in table.items():
            x0, y0, x1, y1 = box
            if x0 <= x < x1 and y0 <= y < y1:
                return name
        return None

    def _slide(self, e):
        (x0, _, x1, _), setter, _done = self.sliders[self.sliding]
        setter(max(0.0, min(1.0, (e.x - x0) / max(1, x1 - x0))))
        self.redraw_soon()

    def _press(self, e):
        self.win.focus_force()
        for name, (box, _, _) in self.sliders.items():
            x0, y0, x1, y1 = box
            if x0 - self.u(6) <= e.x < x1 + self.u(6) and y0 - self.u(5) <= e.y < y1 + self.u(5):
                self.sliding = name
                self._slide(e)
                return
        src = self._in(self.drags, e.x, e.y)
        if src:                      # start dragging something (it follows the mouse)
            self.dragging = [self.drags[src][1], e.x, e.y, e.x, e.y]
            return
        name = self._hit(e)
        if name:
            self.down = name
            self.redraw()
        elif e.y < self.u(23):       # the title strip moves the window
            self._grab = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag(self, e):
        if self.sliding:
            self._slide(e)
            return
        if self.dragging:
            self.dragging[3], self.dragging[4] = e.x, e.y
            self.redraw()
            return
        if self._grab:
            self.win.geometry(f"+{e.x_root - self._grab[0]}+{e.y_root - self._grab[1]}")

    def _release(self, e):
        if self.sliding:
            done = self.sliders.get(self.sliding, (None, None, None))[2]
            self.sliding = None
            if done:
                done()
            return
        if self.dragging:
            payload, sx, sy, _, _ = self.dragging
            self.dragging = None
            if abs(e.x - sx) + abs(e.y - sy) > 4:
                target = self._in(self.drops, e.x, e.y)
                if target:
                    self.drops[target][1](payload)
                else:
                    self.dropped_nowhere(payload)
            else:
                self.clicked_drag_source(payload)
            if not self.closed:
                self.redraw()
            return
        if self._grab:
            self._grab = None
            self.moved()
            return
        name, self.down = self.down, None
        if name and name == self._hit(e) and name in self.buttons:
            self.buttons[name][1]()
        if not self.closed:
            self.redraw()

    def on_wheel(self, e):
        pass

    def moved(self):
        pass

    def dropped_nowhere(self, payload):
        pass

    def clicked_drag_source(self, payload):
        pass

    def drop_target(self):
        """The drop target under the mouse while dragging, if any."""
        if not self.dragging:
            return None
        return self._in(self.drops, self.dragging[3], self.dragging[4])

    # --- drawing
    def redraw_soon(self):
        """Redraw once things settle: a burst of wheel / drag events draws only the last state."""
        if getattr(self, "_rd_pending", False) or self.closed:
            return
        self._rd_pending = True

        def go():
            self._rd_pending = False
            self.redraw()
        self.win.after_idle(go)

    def redraw(self):
        if self.closed:
            return
        try:
            img = self.draw()
        except tk.TclError:
            return
        self._photo = ImageTk.PhotoImage(to_keyed(img), master=self.canvas)
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw", tags="bg")
        self.canvas.tag_lower("bg")

    def button(self, img, name, box, pal, text, action, enabled=True):
        box = tuple(int(round(v)) for v in box)
        if not enabled:
            glossy_button(img, box, BTN_OFF, text, self.k)
            return
        self.buttons[name] = (box, action)
        st = "down" if self.down == name else ("hover" if self.hover == name else None)
        glossy_button(img, box, pal, text, self.k, st)

    def draw(self):
        W, H, u = self.Wp, self.Hp, self.u
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        R = u(5)
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=FRAME_FILL, outline=FRAME_OUT)
        d.rounded_rectangle([1, 1, W - 2, H - 2], radius=R - 1, outline=FRAME_LIGHT)
        d.rounded_rectangle([u(4), u(4), W - u(5), u(21)], radius=u(3), fill=WHITE, outline=TITLE_EDGE)
        d.text((u(10), u(16.5)), self.TITLE, font=font("tahoma", u(9)), fill=(40, 44, 56), anchor="ls")
        self.buttons, self.drags, self.drops, self.sliders = {}, {}, {}, {}
        self.button(img, "close", (W - u(21), u(6.5), W - u(7.5), u(19)), BTN_BLUE, "X", self.close)
        self.draw_body(img)
        return img

    def draw_body(self, img):
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class PromptWindow(MapleWindow):
    """A small game-style question box: a message, an optional text box, OK / CANCEL.
    on_ok(text) returns an error message to show, or None to close."""
    W = 280

    def __init__(self, app, title, message, on_ok, initial=None, ok_text="OK", ok_pal=BTN_BLUE,
                 parent=None):
        self.TITLE, self.message, self.on_ok = title.upper(), message, on_ok
        self.ok_text, self.ok_pal, self.error = ok_text, ok_pal, ""
        self.has_entry = initial is not None
        self.H = 128 if self.has_entry else 104
        super().__init__(app)
        if self.has_entry:
            self.ent, self.var, _ = self.entry((self.u(14), self.u(66), self.Wp - self.u(14), self.u(84)),
                                               initial, max_len=24)
            self.ent.bind("<Return>", lambda e: self.ok())
            self.ent.select_range(0, "end")
        self.win.bind("<Return>", lambda e: self.ok())
        self.centre_on(parent)
        self.redraw()
        self.win.focus_force()
        if self.has_entry:
            self.ent.focus_set()

    def ok(self):
        err = self.on_ok(self.var.get() if self.has_entry else None)
        if err:
            self.error = err
            self.redraw()
        else:
            self.close()

    def draw_body(self, img):
        W, H, u = self.Wp, self.Hp, self.u
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([u(4), u(25), W - u(5), H - u(5)], radius=u(3), fill=BODY_FILL,
                            outline=(120, 150, 170))
        f = font("bold", u(9))
        y = u(40)
        for line in wrap_text(d, self.message, f, W - u(28))[:3]:
            d.text((u(14), y), line, font=f, fill=WHITE, anchor="ls", stroke_width=max(1, u(1)),
                   stroke_fill=(38, 62, 128))
            y += u(12)
        if self.error:
            d.text((u(14), H - u(31) if not self.has_entry else u(96)), self.error, font=font("bold", u(8)),
                   fill=(255, 96, 80), anchor="ls", stroke_width=max(1, u(1)), stroke_fill=BLACK)
        by0, by1 = H - u(26), H - u(10)
        self.button(img, "ok", (W - u(14) - u(150), by0, W - u(14) - u(80), by1), self.ok_pal,
                    self.ok_text, self.ok)
        self.button(img, "cancel", (W - u(14) - u(70), by0, W - u(14), by1), BTN_GREY, "CANCEL", self.close)


def slider(d, box, value, k, active=False):
    """The game's options slider: a white rounded track with a blue thumb."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    d.rounded_rectangle([x0, y0, x1, y1], radius=h // 2, fill=(250, 252, 255), outline=(176, 184, 196))
    d.line([(x0 + h // 2, y0 + 1), (x1 - h // 2, y0 + 1)], fill=(214, 220, 230))
    tw, th = int(round(17 * k)), int(round(10 * k))
    cx = x0 + (x1 - x0) * value
    cy = (y0 + y1) / 2
    tb = [cx - tw / 2, cy - th / 2, cx + tw / 2, cy + th / 2]
    d.rounded_rectangle(tb, radius=int(3 * k), fill=(20, 38, 62))
    top, bot = ((120, 170, 220), (52, 98, 154)) if not active else ((150, 196, 240), (70, 120, 180))
    inner = [tb[0] + 1, tb[1] + 1, tb[2] - 1, tb[3] - 1]
    d.rounded_rectangle(inner, radius=int(2.5 * k), fill=bot)
    d.rounded_rectangle([inner[0], inner[1], inner[2], (inner[1] + inner[3]) / 2], radius=int(2.5 * k), fill=top)
    for i in (-2, 0, 2):                      # the grip lines
        gx = cx + i * k
        d.line([(gx, cy - th * 0.22), (gx, cy + th * 0.22)], fill=(200, 222, 244))


OV_BTN = {   # the game's small window buttons: light top, dark bottom, dark edge, white glyph
    "gear": ((120, 196, 250), (36, 120, 214), (14, 52, 110)),
    "play": ((140, 226, 90), (52, 156, 28), (20, 76, 8)),
    "pause": ((255, 196, 80), (232, 128, 10), (120, 60, 0)),
    "stop": ((255, 120, 110), (214, 40, 36), (110, 14, 12)),
    "off": ((214, 220, 228), (156, 164, 178), (90, 98, 112)),
}


def overlay_button(img, box, kind, glyph, hover=False):
    """A small square game-style button with a white glyph (gear, play, pause or stop)."""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    top, bot, edge = OV_BTN[kind]
    if hover:
        top, bot = shade(top, 0.15), shade(bot, 0.15)
    s = x1 - x0
    r = max(2, int(s * 0.18))
    d.rounded_rectangle(box, radius=r, fill=edge)
    d.rounded_rectangle([x0 + 1, y0 + 1, x1 - 1, y1 - 1], radius=r - 1, fill=bot)
    d.rounded_rectangle([x0 + 1, y0 + 1, x1 - 1, y0 + s * 0.5], radius=r - 1, fill=top)
    d.line([(x0 + r, y0 + 1.5), (x1 - r, y0 + 1.5)], fill=shade(top, 0.35))     # the shine
    cx, cy, g = (x0 + x1) / 2, (y0 + y1) / 2, s * 0.27
    white, dark = (255, 255, 255), edge
    if glyph == "play":
        pts = [(cx - g * 0.75, cy - g), (cx + g, cy), (cx - g * 0.75, cy + g)]
        d.polygon(pts, fill=white, outline=dark)
    elif glyph == "pause":
        w = g * 0.55
        for bx in (cx - g * 0.75, cx + g * 0.2):
            d.rectangle([bx, cy - g, bx + w, cy + g], fill=white, outline=dark)
    elif glyph == "stop":
        d.rectangle([cx - g * 0.85, cy - g * 0.85, cx + g * 0.85, cy + g * 0.85], fill=white, outline=dark)
    elif glyph == "gear":
        import math
        teeth, ro, ri = 6, g * 1.25, g * 0.86
        pts = []
        for i in range(teeth):
            a0 = math.pi * 2 * i / teeth - math.pi / 2
            for da, rr in ((-0.52, ri), (-0.24, ro), (0.24, ro), (0.52, ri)):     # a square-ish tooth
                pts.append((cx + rr * math.cos(a0 + da), cy + rr * math.sin(a0 + da)))
        d.polygon(pts, fill=white, outline=dark)
        h = g * 0.42
        d.ellipse([cx - h, cy - h, cx + h, cy + h], fill=bot, outline=dark)


def checkbox(d, x, y, size, on, hover=False):
    """A game-style tick box."""
    d.rounded_rectangle([x, y, x + size, y + size], radius=max(1, size // 6),
                        fill=(236, 244, 255) if hover else WHITE, outline=NAVY, width=max(1, size // 10))
    if on:
        w = max(2, size // 6)
        d.line([(x + size * 0.2, y + size * 0.52), (x + size * 0.42, y + size * 0.75),
                (x + size * 0.82, y + size * 0.24)], fill=(46, 150, 30), width=w, joint="curve")


def paste_icon(img, item_id, x, y, size):
    ic = item_icon(item_id)
    if ic is None:
        d = ImageDraw.Draw(img)            # a plain bottle for items without an icon
        d.ellipse([x + size * 0.2, y + size * 0.35, x + size * 0.8, y + size * 0.95], fill=(200, 80, 90),
                  outline=BLACK)
        d.rectangle([x + size * 0.4, y + size * 0.12, x + size * 0.6, y + size * 0.38], fill=(230, 230, 236),
                    outline=BLACK)
        return
    img.alpha_composite(ic.resize((int(size), int(size)), Image.LANCZOS), (int(x), int(y)))


def profit_text(v):
    return ("+" if v >= 0 else "-") + fmt_int(abs(v))


class MapSuggest:
    """The drop-down under the MAP box: maps whose name (or street / area) matches what's typed,
    from NiaDB's map list (saved in meowdb_cache/), towns left out, your recent maps first.
    Up / Down to choose, Enter or a click to pick, Esc to close. Anything typed still works as a
    map name even when it isn't in the list."""
    ROWS = 8

    def __init__(self, owner, entry, var):
        self.owner, self.entry, self.var = owner, entry, var
        self.top = None
        self.matches = []
        self.picking = False
        entry.bind("<KeyRelease>", self._typed, add="+")
        entry.bind("<Down>", lambda e: self._move(1))
        entry.bind("<Up>", lambda e: self._move(-1))
        entry.bind("<Return>", self._enter, add="+")
        entry.bind("<Escape>", lambda e: self.close() or "break")
        entry.bind("<FocusOut>", lambda e: entry.after(200, self._focus_left))

    def _focus_left(self):
        try:
            f = self.entry.focus_get()
        except (tk.TclError, KeyError):
            f = None
        if self.top is not None and f is not self.lb:
            self.close()

    def _typed(self, e):
        if e.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        self.refresh()

    def search(self, text):
        app = self.owner.app
        words = text.lower().split()
        if not words or not app.maps:
            return []
        recent = {}
        for i, e in enumerate(reversed(app.profile.get("logs", []))):
            recent.setdefault(e["map"], i)
        hits = []
        for m in app.maps:
            if m["town"]:
                continue
            hay = f"{m['name']} {m['street']} {m['city']} {m['region_name']}".lower()
            if all(w in hay for w in words):
                hits.append(m)
        name_l = text.lower().strip()
        hits.sort(key=lambda m: (m["name"] not in recent, recent.get(m["name"], 0),
                                 not m["name"].lower().startswith(name_l), m["name"]))
        return hits[:40]

    def refresh(self):
        self.matches = self.search(self.var.get())
        if not self.matches or (len(self.matches) == 1 and self.matches[0]["name"] == self.var.get()):
            self.close()
            return
        if self.top is None:
            self._open()
        self.lb.delete(0, "end")
        for m in self.matches:
            area = m["city"].title() if m["city"] and m["city"] != "other" else m["region_name"]
            self.lb.insert("end", f"{m['name']}   -   {m['street']}, {area}")
        self.lb.selection_clear(0, "end")
        self.lb.configure(height=min(self.ROWS, len(self.matches)))
        self._place()

    def _open(self):
        k = self.owner.k
        self.top = tk.Toplevel(self.owner.win)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        frame = tk.Frame(self.top, bg="#162d5a", bd=0, highlightthickness=0)
        frame.pack(fill="both", expand=True)
        self.lb = tk.Listbox(frame, font=("Arial", -int(round(9.4 * k)), "bold"), bg="#ffffff", fg="#1d2b45",
                             selectbackground="#4f7fd0", selectforeground="#ffffff", activestyle="none",
                             highlightthickness=0, bd=0, relief="flat", exportselection=False)
        self.lb.pack(fill="both", expand=True, padx=1, pady=(1, 0))
        tk.Label(frame, text=MEOW_CREDIT, font=("Arial", -int(round(7.2 * k))), bg="#e8f0fa", fg="#46566e",
                 anchor="w").pack(fill="x", padx=1, pady=(0, 1))
        self.lb.bind("<ButtonRelease-1>", lambda e: self.pick())
        self.lb.bind("<Motion>", lambda e: (self.lb.selection_clear(0, "end"),
                                            self.lb.selection_set(self.lb.nearest(e.y))))
        exclude_from_capture(top_hwnd(self.top), self.owner.app.cfg.get("hide_from_screenshots"))

    def _place(self):
        self.top.update_idletasks()
        x, y = self.entry.winfo_rootx(), self.entry.winfo_rooty() + self.entry.winfo_height()
        self.top.geometry(f"{max(self.entry.winfo_width(), int(300 * self.owner.k))}x{self.top.winfo_reqheight()}"
                          f"+{x}+{y}")

    def _move(self, step):
        if self.top is None:
            self.refresh()
            return "break"
        n = self.lb.size()
        cur = self.lb.curselection()
        i = (cur[0] + step) if cur else (0 if step > 0 else n - 1)
        i = max(0, min(n - 1, i))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        self.lb.see(i)
        return "break"

    def _enter(self, e):
        if self.top is not None and self.lb.curselection():
            self.pick()
            return "break"
        self.close()
        return None

    def pick(self):
        cur = self.lb.curselection() if self.top is not None else ()
        if cur:
            m = self.matches[cur[0]]
            self.var.set(m["name"])
            self.owner.app.map_id = m["id"]
            self.entry.icursor("end")
        self.close()

    def close(self):
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None


def is_quest_item(item_id, info=None):
    """Quest items can't be sold or traded: NiaDB marks them untradeable; without that, item IDs
    403xxxx are the game's quest items."""
    if info and (info.get("untradeable") or "quest" in (info.get("category") or info.get("cat") or "").lower()):
        return True
    return bool(item_id) and 4030000 <= int(item_id) < 4040000


class MapInfoWindow(MapleWindow):
    """A map's monsters, and each monster's drops as players saw them in MS Classic (votes), what it's worth (your price or the
    NPC's) and whether it counts toward PROFIT. Quest items are left out. Data: NiaDB, from what's
    cached - saved pages in meowdb_import/, or looked up when that's allowed."""
    W, H = 384, 300

    def __init__(self, app, m):
        self.map = m
        self.TITLE = m["name"].upper()
        super().__init__(app)
        self.expanded = set()
        self.scroll = 0
        self.mobs = None                  # [{"id", "name", "count"}] or None (unknown)
        self.mob_drops = {}                   # mob id -> [drops] or None
        self.items = {}                   # item page -> info
        par = app.controls.win if app.controls and not app.controls.closed else None
        self.centre_on(par)
        self.load()
        self.win.focus_force()

    def load(self):
        app = self.app
        app.import_pages()
        app.meow.map_monsters(self.map["id"], self._got_mobs)

    def _soon(self):
        """One redraw once the burst of cached lookups is in, not one per drop."""
        if self.closed or getattr(self, "_redraw_due", False):
            return
        self._redraw_due = True

        def go():
            self._redraw_due = False
            if not self.closed:
                self.redraw()
        self.win.after_idle(go)

    def _got_mobs(self, mobs):
        self.mobs = mobs
        for mob in mobs or []:
            self.app.meow.drops(mob["id"], lambda d, i=mob["id"]: self._got_drops(i, d))
        self._soon()

    def _got_drops(self, mob_id, drops):
        self.mob_drops[mob_id] = drops
        for dr in drops or []:
            self.app.meow.item(dr["page"], lambda info, pg=dr["page"]: self._got_item(pg, info))
        self._soon()

    def _got_item(self, page, info):
        if info:
            self.items[page] = info
            if info.get("npc") is not None and info.get("name"):
                self.app.prices.entry(info["name"])["npc"] = info["npc"]
        self._soon()

    def toggle(self, mob_id):
        self.expanded ^= {mob_id}
        self.redraw()

    def on_wheel(self, e):
        self.scroll -= int(e.delta / 120 * self.u(24))
        self.redraw_soon()

    def draw_body(self, img):
        W, H, u, app = self.Wp, self.Hp, self.u, self.app
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        d.rounded_rectangle([u(4), u(25), W - u(5), H - u(5)], radius=u(3), fill=BODY_FILL, outline=(120, 150, 170))
        m = self.map
        area = m["city"].title() if m.get("city") and m["city"] != "other" else m.get("region_name", "")
        d.text((u(12), u(39)), f"{m['street']}, {area}", font=font("bold", u(8.6)), fill=WHITE, anchor="ls",
               stroke_width=st, stroke_fill=(38, 62, 96))
        self.button(img, "reload", (W - u(12) - u(70), u(28), W - u(12), u(43)), BTN_BLUE, "RELOAD", self.load)
        lx0, ly0, lx1, ly1 = u(10), u(48), W - u(11), H - u(38)
        d = ImageDraw.Draw(img)
        d.rectangle([lx0, ly0, lx1, ly1], fill=LIST_BG, outline=NAVY)
        if not self.mobs:
            msg = ("Loading..." if self.mobs is None and app.meow.online else
                   f"This map's monsters aren't saved yet. Open this map on meowdb.com, press Ctrl+S "
                   f"(Webpage, HTML only) into the meowdb_import folder, then press RELOAD. Map ID: {m['id']}")
            y = ly0 + u(20)
            for line in wrap_text(d, msg, font("bold", u(8.4)), lx1 - lx0 - u(24)):
                d.text((lx0 + u(12), y), line, font=font("bold", u(8.4)), fill=(70, 86, 110), anchor="ls")
                y += u(12)
        else:
            self._draw_list(img, (lx0, ly0, lx1, ly1))
        d = ImageDraw.Draw(img)
        d.text((u(12), H - u(24)), "▲ = player votes that it drops in Classic. Untick to leave it out of PROFIT.", font=font("bold", u(7.4)), fill=(34, 56, 92), anchor="ls")
        d.text((u(12), H - u(13)), MEOW_CREDIT, font=font("reg", u(7.4)), fill=(70, 86, 110), anchor="ls")

    def _draw_list(self, img, box):
        u, app = self.u, self.app
        lx0, ly0, lx1, ly1 = box
        vx0, vy0, vx1, vy1 = lx0 + 1, ly0 + 1, lx1 - u(6), ly1
        vw, vh = vx1 - vx0, vy1 - vy0
        rows = []
        for mob in self.mobs:
            rows.append(("mob", mob))
            if mob["id"] in self.expanded:
                drops = self.mob_drops.get(mob["id"])
                if drops is None:
                    rows.append(("note", "Drops not saved yet: open this monster on meowdb.com, Ctrl+S into "
                                         "meowdb_import, then RELOAD."))
                    continue
                quests = 0
                for dr in drops:
                    if is_quest_item(dr["id"], self.items.get(dr["page"])) or "quest" in dr.get("cat", "").lower():
                        quests += 1
                        continue
                    rows.append(("drop", dr))
                if quests:
                    rows.append(("note", f"{quests} quest item{'s' if quests != 1 else ''} left out"))
        MOB_H, DROP_H, NOTE_H = u(21), u(20), u(15)
        hgt = {"mob": MOB_H, "drop": DROP_H, "note": NOTE_H}
        total = sum(hgt[k] for k, _ in rows)
        self.scroll = max(0, min(self.scroll, total - vh))
        layer = Image.new("RGBA", (vw, max(total, 1)), (0, 0, 0, 0))
        prof = app.cfg["profit"]
        st = max(1, u(1.1))
        y, buttons = 0, []
        for kind, data in rows:
            h = hgt[kind]
            top = vy0 + y - self.scroll
            visible = top >= vy0 - 1 and top + h <= vy1 + 1
            ld = ImageDraw.Draw(layer)
            if kind == "mob":
                hov = self.hover == f"mob:{data['id']}"
                grad_rrect(layer, (0, y, vw, y + h - 1), 0, shade(PANEL_TOP, 0.12 if hov else 0),
                           shade(PANEL_BOT, 0.12 if hov else 0))
                ld = ImageDraw.Draw(layer)
                tx, ty = u(7), y + h / 2
                if data["id"] in self.expanded:
                    ld.polygon([(tx, ty - u(2.5)), (tx + u(7), ty - u(2.5)), (tx + u(3.5), ty + u(3))], fill=WHITE)
                else:
                    ld.polygon([(tx + u(1), ty - u(3.5)), (tx + u(1), ty + u(3.5)), (tx + u(6.5), ty)], fill=WHITE)
                ld.text((u(18), y + h / 2 + u(3.5)), data["name"], font=font("bold", u(9.2)), fill=WHITE, anchor="ls",
                        stroke_width=st, stroke_fill=(20, 36, 84))
                drops = self.mob_drops.get(data["id"])
                info = (f"{data['count']} here  ·  " if data.get("count") else "") + (
                    (f"{len(drops)} drops" + ("" if drops and "up" in drops[0] else " (reference)"))
                    if drops is not None else "drops not saved")
                ld.text((vw - u(7), y + h / 2 + u(3)), info, font=font("bold", u(7.8)), fill=WHITE, anchor="rs",
                        stroke_width=st, stroke_fill=(20, 36, 84))
                if visible:
                    self.buttons[f"mob:{data['id']}"] = ((vx0, top, vx1, top + h),
                                                         (lambda i=data["id"]: self.toggle(i)))
            elif kind == "note":
                ld.text((u(22), y + u(10.5)), fit_text(ld, data, font("bold", u(7.6)), vw - u(30)),
                        font=font("bold", u(7.6)), fill=(110, 122, 144), anchor="ls")
            else:
                dr = data
                info = self.items.get(dr["page"])
                name = (info or {}).get("name") or dr["name"]
                e = app.prices.items.get(name, {})
                kind_ = "equip" if (info or {}).get("equip") or str(dr["id"]).startswith("1") else \
                    loot_kind(name, "Use" if str(dr["id"]).startswith("2") else
                              "Setup" if str(dr["id"]).startswith("3") else "Etc")
                counted = e.get("include", True) and prof.get(kind_, True)
                ld.rectangle([0, y, vw, y + h - 1], fill=LIST_ALT if len(buttons) % 2 else LIST_BG)
                ic = item_icon(dr["id"])
                if ic is not None:
                    layer.alpha_composite(ic.resize((u(15), u(15)), Image.LANCZOS), (u(20), y + u(2)))
                else:
                    ld.rounded_rectangle([u(21), y + u(3), u(34), y + u(16)], radius=u(2), fill=(226, 232, 242),
                                         outline=(170, 180, 196))
                ld = ImageDraw.Draw(layer)
                ink = INK if counted else (150, 158, 172)
                ld.text((u(40), y + u(13)), fit_text(ld, name, font("bold", u(8.2)), u(116)), font=font("bold", u(8.2)),
                        fill=ink, anchor="ls")
                if "up" in dr:      # MS Classic community drops: votes, not chances
                    ld.text((u(190), y + u(13)), f"▲{dr['up']:g}", font=font("bold", u(8)), fill=(40, 120, 50),
                            anchor="rs")
                else:
                    ld.text((u(190), y + u(13)), dr.get("chance") or "", font=font("bold", u(8)),
                            fill=(80, 96, 124), anchor="rs")
                buttons.append((top, h, name, kind_, e.get("include", True), visible))
            y += h
        img.alpha_composite(layer.crop((0, self.scroll, vw, self.scroll + vh)), (vx0, vy0))
        d = ImageDraw.Draw(img)
        for top, h, name, kind_, inc, visible in buttons:
            if not visible:
                continue
            unit, src = app.prices.unit(name)
            label = (fmt_int(unit) + (" NPC" if src == "NPC" else "")) if src else "SET PRICE"
            self.button(img, f"mp:{name}", (vx0 + u(198), top + u(3), vx0 + u(262), top + u(17)),
                        BTN_GREY if src else BTN_ORANGE, label, (lambda n=name: app.ask_item_price(n)))
            d = ImageDraw.Draw(img)
            cb = (vx1 - u(19), top + u(4), vx1 - u(6), top + u(17))
            checkbox(d, cb[0], cb[1], u(12), inc, self.hover == f"mi:{name}")
            self.buttons[f"mi:{name}"] = (tuple(int(v) for v in cb), (lambda n=name: (app.toggle_item(n), self.redraw())))
        if total > vh:
            th = max(u(12), int(vh * vh / total))
            ty0 = vy0 + int((vh - th) * self.scroll / max(1, total - vh))
            d.rectangle([lx1 - u(4), ty0, lx1 - 1, ty0 + th], fill=(60, 110, 200))


class TrackerWindow(MapleWindow):
    """The tracker window. Tabs: TRACKER (map name, start / pause / stop, numbers, resets), LOG
    (saved sessions for the current character, grouped by map), POTIONS (which potions to count
    and where they are) and SETTINGS (which trackers are on). The character picker is in the
    title strip."""
    W, H = 384, 280
    TABS = (("tracker", "TRACKER"), ("log", "LOG"), ("loot", "LOOT"), ("potions", "POTIONS"),
            ("settings", "SETTINGS"))

    def __init__(self, app):
        super().__init__(app)
        self.tab = app.cfg.get("window_tab", "tracker")
        if self.tab not in dict(self.TABS):
            self.tab = "tracker"
        self.scroll = 0
        self.expanded = set(app.cfg.get("log_expanded", []))
        self.s = None
        self._sig = None
        self._del_boxes = []
        self.qs_pic, self._panel_at = None, 0.0     # picture of the game's quick slots
        self._hover_drag = None
        self.map_ent, self.map_var, self.map_item = self.entry(
            (self.u(46), self.u(48), self.Wp - self.u(160), self.u(64)), app.session.map,
            on_change=app.set_map)
        self.map_suggest = MapSuggest(self, self.map_ent, self.map_var)
        self.loot_query = ""
        self.find_ent, self.find_var, self.find_item = self.entry(
            (self.Wp - self.u(150), self.u(47), self.Wp - self.u(12), self.u(62)), "",
            on_change=self._loot_search, max_len=30)
        g = app.cfg.get("controls_geometry")
        if g:
            self.win.geometry(g)
        else:
            self.centre_on()
        self._show_entry()
        self.win.focus_force()

    def moved(self):
        self.app.cfg["controls_geometry"] = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
        save_json(CONFIG_PATH, self.app.cfg)

    def set_tab(self, tab):
        self.map_suggest.close()
        self.tab = tab
        self.scroll = 0
        self.app.cfg["window_tab"] = tab
        self._show_entry()
        self._sig = None
        self.redraw()

    def _show_entry(self):
        self.canvas.itemconfigure(self.map_item, state="normal" if self.tab == "tracker" else "hidden")
        self.canvas.itemconfigure(self.find_item, state="normal" if self.tab == "loot" else "hidden")

    def _loot_search(self, text):
        self.loot_query = text.strip().lower()
        self.scroll = 0
        self.redraw()

    def set_map_text(self, text):
        if self.map_var.get() != text:
            self.map_var.set(text)

    def on_wheel(self, e):
        if self.tab in ("log", "loot", "settings"):
            self.scroll -= int(e.delta / 120 * self.u(24))
            self._sig = None
            self.redraw_soon()

    def render(self, s):
        self.s = s
        if self.tab == "potions" and not self.dragging and time.time() - self._panel_at > 2.0:
            self._panel_at = time.time()
            pic = self.app.capture_quickslots()
            if pic is not None:
                self.qs_pic = pic
        if self.dragging:
            return                     # the drag redraws as the mouse moves
        # the log and settings tabs only change when their data does; don't redraw every tick
        if self.tab in ("log", "settings"):
            sig = (self.tab, self.app.logs_version, str(self.app.cfg.get("trackers")), str(self.app.cfg.get("profit")),
                   self.app.cfg.get("auto_profile"), self.app.cfg.get("close_with_game"),
                   self.app.cfg.get("hide_from_screenshots"))
            if sig == self._sig:
                return
            self._sig = sig
        else:
            self._sig = None
        self.redraw()

    def redraw(self):
        if self.s is not None:
            super().redraw()

    def toggle_map(self, name):
        if name in self.expanded:
            self.expanded.discard(name)
        else:
            self.expanded.add(name)
        self.app.cfg["log_expanded"] = sorted(self.expanded)
        self._sig = None
        self.redraw()

    # --- drawing
    def draw_body(self, img):
        W, H, u, app = self.Wp, self.Hp, self.u, self.app
        d = ImageDraw.Draw(img)
        # character picker, in the title strip
        cx1 = W - u(26)
        cx0 = cx1 - u(104)
        cbox = (cx0, u(6.5), cx1, u(19))
        hov = self.hover == "profile"
        d.rounded_rectangle(cbox, radius=u(3), fill=(236, 244, 255) if hov else (246, 250, 254), outline=NAVY)
        d.text((cx0 + u(5), u(16.3)), fit_text(d, app.profile["name"], font("bold", u(8.6)), u(84)),
               font=font("bold", u(8.6)), fill=INK, anchor="ls")
        ax, ay = cx1 - u(9), u(11)
        d.polygon([(ax - u(3.2), ay), (ax + u(3.2), ay), (ax, ay + u(3.6))], fill=INK)
        d.text((cx0 - u(5), u(16.3)), "CHARACTER", font=font("tahoma", u(7.5)), fill=(80, 92, 116), anchor="rs")
        self.buttons["profile"] = (cbox, lambda: app.profile_menu(self.win.winfo_rootx() + cx0,
                                                                  self.win.winfo_rooty() + cbox[3]))
        # tabs, with the active one joined to the body
        tw, tp = u(60), u(62)
        for i, (tab, label) in enumerate(self.TABS):
            x0 = u(8) + i * tp
            box = (x0, u(25), x0 + tw, u(42))
            active = self.tab == tab
            top, bot = (BODY_FILL, BODY_FILL) if active else ((122, 150, 168), (104, 132, 150))
            grad_rrect(img, (box[0], box[1], box[2], box[3] + u(4)), u(3), top, bot,
                       outline=(120, 150, 170) if active else (88, 112, 128))
            if not active:
                self.buttons[f"tab_{tab}"] = (box, (lambda t=tab: self.set_tab(t)))
            d = ImageDraw.Draw(img)
            col = WHITE if active else ((236, 242, 248) if self.hover == f"tab_{tab}" else (206, 218, 228))
            d.text(((box[0] + box[2]) / 2, u(37)), label, font=font("black", u(8)), fill=col,
                   anchor="ms", stroke_width=max(1, u(1)), stroke_fill=(38, 62, 96))
        d.rounded_rectangle([u(4), u(41), W - u(5), H - u(22)], radius=u(3), fill=BODY_FILL,
                            outline=(120, 150, 170))
        ai = [t for t, _ in self.TABS].index(self.tab)
        ax0 = u(8) + ai * tp
        d.line([ax0 + 1, u(41), ax0 + tw - 2, u(41)], fill=BODY_FILL)
        getattr(self, "draw_" + self.tab)(img)
        d = ImageDraw.Draw(img)
        hint = "The overlay's buttons: gear = this window, play / pause, stop  ·  drag the overlay to move it"
        d.text((u(9), H - u(8.5)), hint, font=font("reg", u(7.6)), fill=(70, 86, 110), anchor="ls")

    def panel(self, img, box, title, coin=False):
        x0, y0, x1, y1 = box
        u = self.u
        grad_rrect(img, box, u(3), PANEL_TOP, PANEL_BOT, outline=NAVY)
        d = ImageDraw.Draw(img)
        d.line([x0 + 2, y0 + 1, x1 - 3, y0 + 1], fill=PANEL_EDGE)
        x = x0 + u(7)
        if coin:
            r = u(5)
            meso_coin(d, x + r, y0 + u(11), r)
            x += 2 * r + u(4)
        text_runs(d, x, y0 + u(15.5), [(title, font("black", u(11)), WHITE)], max(1, u(1.1)))
        return d

    def rows(self, d, x0, x1, y, rows, step=13.5):
        u, st = self.u, max(1, self.u(1.1))
        labf, valf = font("tahoma", u(8.2)), font("bold", u(9.6))
        for j, row in enumerate(rows):
            a, b = row[0], row[1]
            col = row[2] if len(row) > 2 else WHITE
            ry = y + j * u(step)
            vw = d.textlength(b, font=valf)
            d.text((x0 + u(8), ry), fit_text(d, a, labf, x1 - x0 - u(20) - vw), font=labf, fill=(230, 238, 255),
                   anchor="ls", stroke_width=st, stroke_fill=(38, 62, 128))
            d.text((x1 - u(8), ry), b, font=valf, fill=col, anchor="rs", stroke_width=st,
                   stroke_fill=(20, 36, 84))

    def draw_tracker(self, img):
        W, u, app, s = self.Wp, self.u, self.app, self.s
        se = app.session
        on = app.cfg["trackers"]
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        # MAP [..........] [MOBS] [RUNNING]
        d.text((u(12), u(60)), "MAP", font=font("black", u(10)), fill=WHITE, anchor="ls", stroke_width=st,
               stroke_fill=(38, 62, 96))
        known = app.current_map()
        self.button(img, "mobs", (W - u(155), u(48), W - u(111), u(64)), BTN_BLUE, "MOBS",
                    (lambda: app.open_map_info()), enabled=known is not None)
        d = ImageDraw.Draw(img)
        bx0, bx1 = W - u(106), W - u(12)
        col = STATE_COL[se.state]
        grad_rrect(img, (bx0, u(48), bx1, u(64)), u(8), shade(col, 0.25), shade(col, -0.15),
                   outline=shade(col, -0.55))
        d = ImageDraw.Draw(img)
        state_icon(d, bx0 + u(8), u(52), u(8), se.state, WHITE)
        d.text((bx0 + u(22), u(60)), STATE_TEXT[se.state], font=font("black", u(8.4)), fill=WHITE,
               anchor="ls", stroke_width=st, stroke_fill=shade(col, -0.6))
        # START / RESUME, PAUSE, STOP & SAVE
        y0, y1 = u(70), u(88)
        if se.state == PAUSED:
            self.button(img, "start", (u(12), y0, u(92), y1), BTN_GREEN, "RESUME", app.start)
        else:
            self.button(img, "start", (u(12), y0, u(92), y1), BTN_GREEN, "START", app.start,
                        enabled=se.state == STOPPED)
        self.button(img, "pause", (u(97), y0, u(177), y1), BTN_ORANGE, "PAUSE", app.pause,
                    enabled=se.state == RUNNING)
        self.button(img, "stop", (u(182), y0, u(274), y1), BTN_RED, "STOP & SAVE", app.stop,
                    enabled=se.state != STOPPED)
        d = ImageDraw.Draw(img)
        d.text((W - u(12), u(77)), "SESSION", font=font("black", u(7)), fill=(230, 238, 255), anchor="rs",
               stroke_width=st, stroke_fill=(38, 62, 128))
        d.text((W - u(12), u(88)), fmt_dur(se.active), font=font("bold", u(9.6)), fill=WHITE, anchor="rs",
               stroke_width=st, stroke_fill=(20, 36, 84))
        if not se.map.strip():
            hint = "Name the map to save this session to the LOG tab."
        elif se.state == STOPPED:
            hint = "Press START when you're ready to hunt."
        else:
            hint = f"Saves to the LOG when you stop, or in {fmt_dur(max(0, SEGMENT_S - se.active))} (every hour)."
        d.text((u(12), u(100)), hint, font=font("bold", u(8)), fill=(34, 56, 92), anchor="ls")
        # one panel per tracker that's on
        keys = [k for k in ("exp", "meso", "potions") if on.get(k) or (k == "meso" and on.get("loot"))]
        py0, py1 = u(106), u(226)
        if not keys:
            d.text((W / 2, u(165)), "All trackers are off. Turn them on in the SETTINGS tab.",
                   font=font("bold", u(9)), fill=WHITE, anchor="ms", stroke_width=st, stroke_fill=(38, 62, 96))
        gap = u(8)
        pw = (W - u(24) - gap * (len(keys) - 1)) / max(1, len(keys))
        for i, key in enumerate(keys):
            px0 = u(12) + i * (pw + gap)
            px1 = px0 + pw
            if key == "exp":
                d = self.panel(img, (px0, py0, px1, py1), "EXP.")
                if s.get("pct") is not None:
                    exp_gauge(d, px0 + u(8), py0 + u(19), pw - u(16), max(5, u(5)), s["pct"] / 100.0)
                self.rows(d, px0, px1, py0 + u(36), [
                    ("Gained", s["c_exp_total"]), ("Per hour", s["c_exp_rate"]), ("Time", s["c_exp_time"]),
                    ("Next level in", s["c_exp_extra"]), (s["c_exp_extra2_label"], s["c_exp_extra2"])])
                name, label = "reset_exp", "RESET EXP"
            elif key == "meso":
                d = self.panel(img, (px0, py0, px1, py1), "MESO" if on.get("meso") else "LOOT",
                               coin=bool(on.get("meso")))
                profit_row = ("Profit", s["profit"], PROFIT_GREEN if s["profit_v"] >= 0 else PROFIT_RED)
                two = sum(bool(on.get(k)) for k in ("meso", "loot", "potions")) >= 2
                if on.get("meso") and on.get("loot"):
                    rows = [("Mesos", s["c_meso_total"]), ("Per hour", s["c_meso_rate"]),
                            ("Loot", s["c_loot_total"]), ("Items", s["c_loot_items"]), profit_row]
                elif on.get("meso"):
                    rows = [("Gained", s["c_meso_total"]), ("Per hour", s["c_meso_rate"]), ("Time", s["c_meso_time"]),
                            ("Pickups", s["c_meso_extra"]),
                            profit_row if two else ("Average", s["c_meso_extra2"])]
                else:
                    rows = [("Loot", s["c_loot_total"]), ("Per hour", s["looth"]), ("Items", s["c_loot_items"])] + \
                        ([profit_row] if two else [])
                self.rows(d, px0, px1, py0 + u(36), rows)
                name, label = (("reset_mesoloot", "RESET MESO+LOOT") if on.get("meso") and on.get("loot") else
                               ("reset_meso", "RESET MESOS") if on.get("meso") else ("reset_loot", "RESET LOOT"))
            else:
                d = self.panel(img, (px0, py0, px1, py1), "POTIONS")
                pots = app.profile.get("potions", [])
                if not pots:
                    f = font("bold", u(8))
                    y = py0 + u(36)
                    for line in wrap_text(d, "Pick your potions on the POTIONS tab.", f, pw - u(16)):
                        d.text((px0 + u(8), y), line, font=f, fill=WHITE, anchor="ls", stroke_width=st,
                               stroke_fill=(38, 62, 128))
                        y += u(11)
                else:
                    used = se.pots
                    rows = []
                    for p in pots[:3]:
                        n = used.get(p["name"], {}).get("used", 0)
                        rows.append(("      " + p["name"], f"{n}  ·  {fmt_int(n * (p.get('price') or 0))}"))
                    rows += [("Cost", "-" + fmt_int(se.pot_cost), POT_RED), ("Per hour", s["c_pot_rate"])]
                    self.rows(d, px0, px1, py0 + u(36), rows)
                    for j, p in enumerate(pots[:3]):
                        paste_icon(img, p.get("id"), px0 + u(7), py0 + u(36) + j * u(13.5) - u(10), u(11))
                name, label = "reset_potions", "RESET POTIONS"
            self.button(img, name, (px0 + u(8), py1 - u(22), px1 - u(8), py1 - u(7)), BTN_BLUE, label,
                        (lambda kk=name[6:]: app.reset(kk)))
        by0, by1 = u(232), u(252)
        self.button(img, "reset_both", (u(12), by0, u(130), by1), BTN_ORANGE, "RESET ALL",
                    lambda: app.reset("all"))
        self.button(img, "quit", (W - u(12) - u(84), by0, W - u(12), by1), BTN_RED, "QUIT",
                    lambda: app.shutdown("quit"))

    def draw_settings(self, img):
        """The settings, scrolled (mouse wheel or the bar on the right) when they don't all fit."""
        W, H, u = self.Wp, self.Hp, self.u
        vy0, vy1 = u(43), H - u(24)            # inside the panel's border
        vh = vy1 - vy0
        app = self.app
        key = (self.hover, self.sliding,
               json.dumps({k: app.cfg.get(k) for k in ("trackers", "profit", "exp_display", "underlay",
                                                       "underlay_opacity", "auto_profile",
                                                       "close_with_game", "hide_from_screenshots")}, sort_keys=True))
        cache = getattr(self, "_set_cache", None)
        if cache is None or cache[0] != key:
            layer = Image.new("RGBA", (W, u(460)), (0, 0, 0, 0))
            before = {"b": set(self.buttons), "s": set(self.sliders)}
            total = self._settings_body(layer, u(3)) + u(4)
            made = {"b": {n: v for n, v in self.buttons.items() if n not in before["b"]},
                    "s": {n: v for n, v in self.sliders.items() if n not in before["s"]}}
            cache = self._set_cache = (key, layer, total, made)
        _, layer, total, made = cache
        self.scroll = max(0, min(self.scroll, total - vh))
        img.alpha_composite(layer.crop((0, self.scroll, W, self.scroll + vh)), (0, vy0))
        off = vy0 - self.scroll
        for table, mk in ((self.buttons, made["b"]), (self.sliders, made["s"])):
            for name, entry in mk.items():
                table[name] = entry
                box, *rest = table[name]
                nb = (box[0], box[1] + off, box[2], box[3] + off)
                if nb[3] < vy0 + u(4) or nb[1] > vy1 - u(4):
                    del table[name]             # scrolled out of view
                else:
                    table[name] = (nb, *rest)
        if total > vh:                          # the scroll bar, game style
            d = ImageDraw.Draw(img)
            bx0, bx1 = W - u(10), W - u(5)
            d.rounded_rectangle([bx0, vy0, bx1, vy1], radius=u(2), fill=(226, 234, 244), outline=(120, 140, 170))
            th = max(u(16), int(vh * vh / total))
            ty0 = vy0 + int((vh - th) * self.scroll / max(1, total - vh))
            d.rounded_rectangle([bx0, ty0, bx1, ty0 + th], radius=u(2), fill=(52, 98, 154), outline=(20, 38, 62))
            d.line([(bx0 + 1, ty0 + 1), (bx0 + 1, ty0 + th - 1)], fill=(120, 170, 220))
            self.sliders["set_scroll"] = ((bx0, vy0, bx1, vy1),
                                          lambda v: setattr(self, "scroll", int(v * (total - vh))), None)

    def _settings_body(self, img, y):
        """Draws the settings from y down; -> where they end."""
        W, u, app = self.Wp, self.u, self.app
        W -= u(8)                               # room for the scroll bar
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        on = app.cfg["trackers"]

        def heading(text, yy):
            d.text((u(12), yy + u(11)), text, font=font("black", u(9.5)), fill=WHITE, anchor="ls",
                   stroke_width=st, stroke_fill=(38, 62, 96))

        def toggle(name, yy, title, desc, value, action):
            box = (u(12), yy, W - u(12), yy + u(25))
            hov = self.hover == name
            if hov:
                d.rounded_rectangle(box, radius=u(3), fill=(170, 200, 216))
            checkbox(d, u(18), yy + u(6), u(13), value, hov)
            d.text((u(38), yy + u(12)), title, font=font("bold", u(9.4)), fill=WHITE, anchor="ls",
                   stroke_width=st, stroke_fill=(38, 62, 96))
            d.text((u(38), yy + u(23)), desc, font=font("reg", u(7.8)), fill=(30, 50, 84), anchor="ls")
            self.buttons[name] = (box, action)

        def small(name, x, yy, w, title, value, action):
            box = (x, yy, x + w, yy + u(17))
            hov = self.hover == name
            if hov:
                d.rounded_rectangle(box, radius=u(3), fill=(170, 200, 216))
            checkbox(d, x + u(4), yy + u(3), u(11), value, hov)
            d.text((x + u(19), yy + u(12)), title, font=font("bold", u(8.6)), fill=WHITE, anchor="ls",
                   stroke_width=st, stroke_fill=(38, 62, 96))
            self.buttons[name] = (box, action)

        heading("TRACKERS", y)
        y += u(15)
        cw = (W - u(24)) / 4
        for i, (k, title) in enumerate((("exp", "EXP"), ("meso", "Mesos"), ("loot", "Loot"), ("potions", "Potions"))):
            small(f"t_{k}", u(12) + i * cw, y, cw - u(4), title, on.get(k),
                  (lambda kk=k: app.set_tracker(kk, not app.cfg["trackers"].get(kk))))
        y += u(20)
        heading("PROFIT COUNTS", y)
        y += u(15)
        prof = app.cfg["profit"]
        cw = (W - u(24)) / 3
        for i, (k, title) in enumerate((("etc", "Etc items"), ("equip", "Equips"), ("scroll", "Scrolls"),
                                        ("use", "Use items"), ("setup", "Setup items"),
                                        ("free_potions", "Looted potions free"))):
            r, c = divmod(i, 3)
            small(f"p_{k}", u(12) + c * cw, y + r * u(18), cw - u(4), title, prof.get(k, True),
                  (lambda kk=k: app.set_profit(kk, not app.cfg["profit"].get(kk, True))))
        y += u(37)
        d.text((u(14), y + u(6)), "PROFIT = mesos + loot that counts - potions. Loot is priced on the LOOT tab.",
               font=font("bold", u(7.8)), fill=(34, 56, 92), anchor="ls")
        y += u(10)
        heading("OVERLAY", y)
        y += u(15)

        def choice(name, x, yy, w, title, chosen, action):
            box = (x, yy, x + w, yy + u(15))
            hov = self.hover == name
            top, bot = ((96, 172, 240), (30, 96, 170)) if chosen else ((246, 248, 252), (200, 208, 220))
            if hov:
                top, bot = shade(top, 0.12), shade(bot, 0.12)
            d.rounded_rectangle(box, radius=u(3), fill=bot, outline=(40, 60, 96))
            d.rounded_rectangle([box[0] + 1, box[1] + 1, box[2] - 1, (box[1] + box[3]) / 2], radius=u(3), fill=top)
            d.text(((box[0] + box[2]) / 2, yy + u(11)), title, font=font("bold", u(8)),
                   fill=WHITE if chosen else (40, 56, 84), anchor="ms",
                   stroke_width=st if chosen else 0, stroke_fill=(20, 50, 100))
            self.buttons[name] = (box, action)

        def label(text, yy):
            d.text((u(14), yy + u(11)), text, font=font("bold", u(8.6)), fill=WHITE, anchor="ls", stroke_width=st,
                   stroke_fill=(38, 62, 96))

        label("EXP shows", y)
        mode = app.cfg.get("exp_display", "exp")
        for i, (k_, title) in enumerate((("exp", "EXP / h"), ("pct", "% / h"), ("both", "Both"))):
            choice(f"x_{k_}", u(80) + i * u(54), y, u(50), title, mode == k_,
                   lambda kk=k_: app.set_value("exp_display", kk))
        y += u(19)
        label("Background", y)
        style = app.cfg.get("underlay", "off")
        for i, (k_, title) in enumerate((("off", "Off"), ("light", "Light"), ("dark", "Dark"))):
            choice(f"u_{k_}", u(80) + i * u(38), y, u(35), title, style == k_,
                   lambda kk=k_: app.set_value("underlay", kk))
        # opacity: 0% ---[]--- 100%
        sx0, sx1 = u(214), W - u(36)
        op = float(app.cfg.get("underlay_opacity", 0.55))
        dim = (34, 56, 92) if style != "off" else (110, 128, 150)
        d.text((sx0 - u(5), y + u(11)), "0%", font=font("bold", u(7.8)), fill=dim, anchor="rs")
        d.text((sx1 + u(5), y + u(11)), "100%", font=font("bold", u(7.8)), fill=dim, anchor="ls")
        tb = (sx0, y + u(4), sx1, y + u(11))
        slider(d, tb, op, self.k, self.sliding == "u_op")
        self.sliders["u_op"] = (tb, lambda v: app.set_value("underlay_opacity", round(v, 2), save=False),
                                lambda: app.set_value("underlay_opacity", app.cfg["underlay_opacity"]))
        y += u(21)
        heading("OPTIONS", y)
        y += u(15)
        toggle("o_auto", y, "Switch character automatically",
               "Reads your character's name on the status bar and switches to their profile.",
               app.cfg.get("auto_profile"), lambda: app.set_option("auto_profile", not app.cfg.get("auto_profile")))
        y += u(26)
        toggle("o_close", y, "Close with the game", "Quit MapleTracker when MapleStory closes (and save the session).",
               app.cfg.get("close_with_game"), lambda: app.set_option("close_with_game",
                                                                      not app.cfg.get("close_with_game")))
        y += u(26)
        toggle("o_hide", y, "Hide from screenshots",
               "Leaves MapleTracker out of ALL screen capture (yours too: OBS, Discord, Snipping Tool).",
               app.cfg.get("hide_from_screenshots"),
               lambda: app.set_option("hide_from_screenshots", not app.cfg.get("hide_from_screenshots")))
        return y + u(26)

    def draw_loot(self, img):
        """This session's pickups first, then everything else in your price book: price (click to
        set yours), what it adds up to, and whether it counts toward PROFIT."""
        W, H, u, app = self.Wp, self.Hp, self.u, self.app
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        total, rows = app.loot_summary()
        d.text((u(12), u(59)), f"LOOT  {('+' if total >= 0 else '')}{fmt_int(total)}", font=font("black", u(9.5)),
               fill=WHITE, anchor="ls", stroke_width=st, stroke_fill=(38, 62, 96))
        d.text((W - u(154), u(58)), "FIND", font=font("black", u(7.4)), fill=WHITE, anchor="rs", stroke_width=st,
               stroke_fill=(38, 62, 96))
        q = self.loot_query
        prof = app.cfg["profit"]
        session = {r["name"]: r for r in rows}
        others = [n for n in sorted(app.prices.items, key=lambda n: -app.prices.items[n].get("seen", 0))
                  if n not in session]
        items = [r["name"] for r in sorted(rows, key=lambda r: -r["value"])] + others
        if q:
            items = [n for n in items if all(w in n.lower() for w in q.split())]
        lx0, ly0, lx1, ly1 = u(10), u(68), W - u(11), H - u(42)
        d.rectangle([lx0, ly0, lx1, ly1], fill=LIST_BG, outline=NAVY)
        if not items:
            msg = ("Nothing matches." if q else
                   "Nothing picked up yet. Items you loot while the tracker runs show up here - click a price "
                   "to set what it's worth to you.")
            y = ly0 + u(20)
            for line in wrap_text(d, msg, font("bold", u(8.4)), lx1 - lx0 - u(24)):
                d.text((lx0 + u(12), y), line, font=font("bold", u(8.4)), fill=(70, 86, 110), anchor="ls")
                y += u(12)
        RH = u(21)
        vh = ly1 - ly0 - 2
        total_h = len(items) * RH
        self.scroll = max(0, min(self.scroll, total_h - vh))
        first = self.scroll // RH
        y = ly0 + 1 - (self.scroll % RH)
        for i in range(first, len(items)):
            if y > ly1 - RH:
                break
            name = items[i]
            r = session.get(name)
            e = app.prices.items.get(name, {})
            unit, src = (r["unit"], r["src"]) if r else app.prices.unit(name)
            kind = r["kind"] if r else loot_kind(name, e.get("cat"))
            counted = r["included"] if r else (e.get("include", True) and prof.get(kind, True))
            d = ImageDraw.Draw(img)
            d.rectangle([lx0 + 1, y, lx1 - 1, y + RH - 1], fill=LIST_ALT if i % 2 else LIST_BG)
            ink = INK if counted else (150, 158, 172)
            d.text((lx0 + u(7), y + u(10)), fit_text(d, name, font("bold", u(8.4)), u(140)), font=font("bold", u(8.4)),
                   fill=ink, anchor="ls")
            tag = {"potion": "your potion", "scroll": "scroll"}.get(kind, (e.get("cat") or kind).lower())
            if not prof.get(kind, True) and kind != "potion":
                tag += " (off in SETTINGS)"
            d.text((lx0 + u(7), y + u(18.5)), tag, font=font("reg", u(6.8)), fill=(110, 122, 144), anchor="ls")
            d.text((lx0 + u(176), y + u(14)), f"x{r['qty']}" if r else "", font=font("bold", u(8.4)), fill=ink,
                   anchor="rs")
            label = (fmt_int(unit) + {"you": "", "NPC": " NPC", "default": "", "potion": " pot"}.get(src, "")) \
                if src else "SET PRICE"
            if kind == "potion":
                self.button(img, f"ip:{name}", (lx0 + u(182), y + u(3), lx0 + u(246), y + u(17)), BTN_GREY, label,
                            lambda: None, enabled=False)
            else:
                self.button(img, f"ip:{name}", (lx0 + u(182), y + u(3), lx0 + u(246), y + u(17)),
                            BTN_GREY if src else BTN_ORANGE, label, (lambda n=name: app.ask_item_price(n)))
            d = ImageDraw.Draw(img)
            if r:
                d.text((lx1 - u(26), y + u(14)), ("+" if counted else "") + fmt_int(r["value"]),
                       font=font("bold", u(8.6)), fill=(24, 128, 34) if counted else (150, 158, 172), anchor="rs")
            cb = (lx1 - u(20), y + u(4), lx1 - u(7), y + u(17))
            checkbox(d, cb[0], cb[1], u(12), e.get("include", True), self.hover == f"ii:{name}")
            if kind != "potion":
                self.buttons[f"ii:{name}"] = (tuple(int(v) for v in cb), (lambda n=name: app.toggle_item(n)))
            y += RH
        if total_h > vh:
            sx0 = lx1 - u(4)
            th = max(u(12), int(vh * vh / total_h))
            ty0 = ly0 + 1 + int((vh - th) * self.scroll / max(1, total_h - vh))
            d.rectangle([sx0, ty0, lx1 - 1, ty0 + th], fill=(60, 110, 200))
        d = ImageDraw.Draw(img)
        y = H - u(32)
        msg = ("Click a price to set what an item is worth to you (empty = the NPC price). Untick an item - or "
               "switch a kind off in SETTINGS - to leave it out of PROFIT. Item names: NiaDB (meowdb.com).")
        for line in wrap_text(d, msg, font("bold", u(7.6)), W - u(24))[:2]:
            d.text((u(12), y), line, font=font("bold", u(7.6)), fill=(34, 56, 92), anchor="ls")
            y += u(10)

    def draw_potions(self, img):
        """Left: this character's potions (drag one by its icon). Right: a picture of the game's
        quick slots - the player's own keys and items - to drop each potion on the slot it's on."""
        W, H, u, app = self.Wp, self.Hp, self.u, self.app
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        pots = app.profile.get("potions", [])
        d.text((u(12), u(59)), f"{app.profile['name'].upper()}'S POTIONS", font=font("black", u(9)), fill=WHITE,
               anchor="ls", stroke_width=st, stroke_fill=(38, 62, 96))
        self.button(img, "add_potion", (u(226) - u(84), u(47), u(226), u(63)), BTN_GREEN, "ADD POTION",
                    app.open_catalog, enabled=len(pots) < 6)
        dragging = self.dragging[0][0] if self.dragging else None      # the potion being dragged
        target = self.drop_target()
        # --- the list
        lx0, ly0, lx1, ly1 = u(10), u(68), u(226), H - u(42)
        d.rectangle([lx0, ly0, lx1, ly1], fill=LIST_BG, outline=NAVY)
        if not pots:
            f = font("bold", u(8.2))
            y = ly0 + u(18)
            for line in wrap_text(d, "No potions yet. Press ADD POTION and pick what you drink (or type in your "
                                     "own). Then drag it onto the quick slot it sits on, on the right.",
                                  f, lx1 - lx0 - u(20)):
                d.text((lx0 + u(10), y), line, font=f, fill=(70, 86, 110), anchor="ls")
                y += u(11)
        RH = u(27)
        for i, p in enumerate(pots):
            y = ly0 + 1 + i * RH
            d.rectangle([lx0 + 1, y, lx1 - 1, y + RH - 1], fill=LIST_ALT if i % 2 else LIST_BG)
            d.line([lx0 + 1, y + RH - 1, lx1 - 1, y + RH - 1], fill=(206, 218, 234))
            ibox = (lx0 + u(4), y + u(2.5), lx0 + u(26), y + u(24.5))
            hov = self._hover_drag == f"pot:{i}"
            d.rounded_rectangle(ibox, radius=u(3), fill=(255, 250, 220) if hov else WHITE,
                                outline=(60, 200, 40) if p.get("slot") else (230, 150, 30), width=max(1, u(1.2)))
            if dragging != i:
                paste_icon(img, p.get("id"), ibox[0] + u(2), ibox[1] + u(2), u(18))
            self.drags[f"pot:{i}"] = (tuple(int(v) for v in ibox), (i, False))
            d = ImageDraw.Draw(img)
            d.text((lx0 + u(31), y + u(12)), fit_text(d, p["name"], font("bold", u(8.8)), u(76)),
                   font=font("bold", u(8.8)), fill=INK, anchor="ls")
            if not p.get("slot"):
                sub, col = "drag onto its quick slot", (200, 110, 10)
            elif app.pot_now.get(i) is not None:
                sub, col = f"reads {app.pot_now[i]} in the game", (40, 120, 40)
            else:
                sub, col = "on a quick slot", (40, 120, 40)
            d.text((lx0 + u(31), y + u(22.5)), fit_text(d, sub, font("reg", u(7.4)), u(78)), font=font("reg", u(7.4)),
                   fill=col, anchor="ls")
            price = p.get("price")
            self.button(img, f"price:{i}", (lx1 - u(84), y + u(5.5), lx1 - u(24), y + u(21.5)), BTN_GREY,
                        (fmt_int(price) + " MESOS") if price is not None else "SET PRICE",
                        (lambda ii=i: app.ask_potion_price(ii)))
            self.button(img, f"delpot:{i}", (lx1 - u(20), y + u(6.5), lx1 - u(5), y + u(20.5)), BTN_RED, "X",
                        (lambda ii=i: app.remove_potion(ii)))
        # --- the quick slots
        gx0, gy0, gx1, gy1 = u(232), u(47), W - u(10), H - u(42)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([gx0, gy0, gx1, gy1], radius=u(3), fill=(70, 80, 96), outline=NAVY)
        ax0, ay0, ax1, ay1 = gx0 + u(4), gy0 + u(4), gx1 - u(4), gy1 - u(14)
        boxes = {}
        panel = self.qs_pic
        if panel:
            pimg, pboxes = panel
            sc = min((ax1 - ax0) / pimg.width, (ay1 - ay0) / pimg.height)
            pw, ph = int(pimg.width * sc), int(pimg.height * sc)
            ox, oy = ax0 + (ax1 - ax0 - pw) // 2, ay0 + (ay1 - ay0 - ph) // 2
            img.alpha_composite(pimg.convert("RGBA").resize((pw, ph), Image.LANCZOS), (ox, oy))
            for slot, (bx0, by0, bx1, by1) in pboxes.items():
                boxes[slot] = (ox + bx0 * sc, oy + by0 * sc, ox + bx1 * sc, oy + by1 * sc)
            caption = "your quick slots (live)"
        else:
            cw, ch = (ax1 - ax0) / 4, (ay1 - ay0) / 3
            for r in range(3):
                for c in range(4):
                    x0, y0 = ax0 + c * cw + u(2), ay0 + r * ch + u(2)
                    bx = (x0, y0, x0 + cw - u(4), y0 + ch - u(4))
                    d.rounded_rectangle(bx, radius=u(3), fill=(236, 240, 246), outline=(120, 130, 150))
                    boxes[(c, r)] = bx
            caption = "open the game to see your slots"
        d = ImageDraw.Draw(img)
        d.text(((gx0 + gx1) / 2, gy1 - u(4)), caption, font=font("bold", u(7)), fill=(220, 230, 244), anchor="ms")
        on_slot = {tuple(p["slot"]): i for i, p in enumerate(pots) if p.get("slot")}
        for slot, bx in boxes.items():
            name = f"slot:{slot[0]},{slot[1]}"
            self.drops[name] = (tuple(int(v) for v in bx), (lambda pi, s=slot: self.drop_on_slot(pi, s)))
            i = on_slot.get(slot)
            col = (255, 214, 64) if target == name else ((60, 210, 40) if i is not None else None)
            if col:
                d.rounded_rectangle(bx, radius=u(3), outline=col, width=max(2, u(2)))
            if i is not None and dragging != i:
                # the potion's icon in the slot's corner; drag it off to take it out of this slot
                sz = u(11)
                cx0, cy0 = bx[2] - sz - u(1), bx[1] + u(1)
                d.rounded_rectangle([cx0 - 1, cy0 - 1, cx0 + sz + 1, cy0 + sz + 1], radius=u(2), fill=WHITE,
                                    outline=(60, 210, 40))
                paste_icon(img, pots[i].get("id"), cx0, cy0, sz)
                d = ImageDraw.Draw(img)
                self.drags[f"onslot:{i}"] = ((int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3])), (i, True))
        # --- what's being dragged follows the mouse
        if dragging is not None and 0 <= dragging < len(pots):
            mx, my = self.dragging[3], self.dragging[4]
            sz = u(20)
            paste_icon(img, pots[dragging].get("id"), mx - sz // 2, my - sz // 2, sz)
        d = ImageDraw.Draw(img)
        f = font("bold", u(7.8))
        y = H - u(32)
        msg = ("Drag each potion onto the quick slot it's on in your game (drag it off a slot to remove it). "
               "Only drops in the count are counted: loot and shopping never are.")
        for line in wrap_text(d, msg, f, W - u(24))[:2]:
            d.text((u(12), y), line, font=f, fill=(34, 56, 92), anchor="ls")
            y += u(10)

    def drop_on_slot(self, payload, slot):
        self.app.set_potion_slot(payload[0], slot)

    def dropped_nowhere(self, payload):
        i, from_slot = payload
        if from_slot and 0 <= i < len(self.app.profile["potions"]):   # dragged off its slot
            self.app.clear_potion_slot(i)


    def draw_log(self, img):
        W, H, u, app = self.Wp, self.Hp, self.u, self.app
        d = ImageDraw.Draw(img)
        st = max(1, u(1.1))
        logs = app.profile["logs"]
        n = len(logs)
        d.text((u(12), u(59)), f"{app.profile['name'].upper()}  ·  {n} session{'s' if n != 1 else ''} saved",
               font=font("black", u(9)), fill=WHITE, anchor="ls", stroke_width=st, stroke_fill=(38, 62, 96))
        self.button(img, "clear_all", (W - u(12) - u(80), u(47), W - u(12), u(63)), BTN_RED, "CLEAR ALL",
                    app.ask_clear_logs, enabled=bool(logs))
        lx0, ly0, lx1, ly1 = u(10), u(68), W - u(11), H - u(27)
        d = ImageDraw.Draw(img)
        d.rectangle([lx0, ly0, lx1, ly1], fill=LIST_BG, outline=NAVY)
        vx0, vy0, vx1, vy1 = lx0 + 1, ly0 + 1, lx1 - u(7), ly1
        vw, vh = vx1 - vx0, vy1 - vy0
        if not logs:
            f = font("bold", u(8.6))
            msg = ("No logs yet for this character. On the TRACKER tab, type the map's name and press "
                   "START. The session is saved here when you press STOP & SAVE, after every hour of "
                   "tracking, and when the game closes.")
            y = ly0 + u(22)
            for line in wrap_text(d, msg, f, lx1 - lx0 - u(24)):
                d.text((lx0 + u(12), y), line, font=f, fill=(70, 86, 110), anchor="ls")
                y += u(12)
            return
        groups = {}
        for e in logs:
            groups.setdefault(e["map"], []).append(e)
        order = sorted(groups, key=lambda m: max(e["end"] for e in groups[m]), reverse=True)
        MAP_H, SES_H = u(22), u(31)
        self._del_boxes = []
        rows = []
        for m in order:
            rows.append(("map", m, groups[m]))
            if m in self.expanded:
                rows += [("ses", m, e) for e in sorted(groups[m], key=lambda e: e["end"], reverse=True)]
        tall = lambda e: "pot_cost" in e or "profit" in e            # three lines instead of two
        row_h = lambda r: MAP_H if r[0] == "map" else (SES_H + u(11) if tall(r[2]) else SES_H)
        total = sum(row_h(r) for r in rows)
        self.scroll = max(0, min(self.scroll, total - vh))
        layer = Image.new("RGBA", (vw, max(total, 1)), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        y = 0
        alt = False
        good, bad = (24, 128, 34), (196, 40, 40)
        for kind, m, data in rows:
            h = row_h((kind, m, data))
            top = vy0 + y - self.scroll
            visible = top >= vy0 - 1 and top + h <= vy1 + 1
            if kind == "map":
                name = f"map:{m}"
                hov = self.hover == name
                grad_rrect(layer, (0, y, vw, y + h - 1), 0, shade(PANEL_TOP, 0.12 if hov else 0),
                           shade(PANEL_BOT, 0.12 if hov else 0))
                ld = ImageDraw.Draw(layer)
                ld.line([0, y + h - 1, vw, y + h - 1], fill=NAVY)
                tx, ty = u(7), y + h / 2
                if m in self.expanded:
                    ld.polygon([(tx, ty - u(2.5)), (tx + u(7), ty - u(2.5)), (tx + u(3.5), ty + u(3))], fill=WHITE,
                               outline=(20, 36, 84))
                else:
                    ld.polygon([(tx + u(1), ty - u(3.5)), (tx + u(1), ty + u(3.5)), (tx + u(6.5), ty)], fill=WHITE,
                               outline=(20, 36, 84))
                secs = sum(e["active_s"] for e in data)
                bits = [f"{len(data)}x", fmt_dur(secs)]
                ex = [e for e in data if "exp" in e]
                if ex:
                    es = sum(e["active_s"] for e in ex)
                    bits.append(f"{fmt_short(sum(e['exp'] for e in ex) * 3600 / es) if es else 0} EXP/h")
                pr = [e for e in data if "profit" in e]
                ms = [e for e in data if "meso" in e]
                if pr:
                    ps = sum(e["active_s"] for e in pr)
                    v = sum(e["profit"] for e in pr) * 3600 / ps if ps else 0
                    bits.append(("+" if v >= 0 else "-") + f"{fmt_short(abs(v))} profit/h")
                elif ms:
                    mss_ = sum(e["active_s"] for e in ms)
                    bits.append(f"{fmt_short(sum(e['meso'] for e in ms) * 3600 / mss_) if mss_ else 0} mesos/h")
                summ = "  ·  ".join(bits)
                sf = font("bold", u(7.8))
                sw = ld.textlength(summ, font=sf)
                ld.text((vw - u(7), y + h / 2 + u(3)), summ, font=sf, fill=WHITE, anchor="rs", stroke_width=st,
                        stroke_fill=(20, 36, 84))
                nf = font("bold", u(9.4))
                ld.text((u(18), y + h / 2 + u(3.5)), fit_text(ld, m, nf, vw - u(30) - sw), font=nf, fill=WHITE,
                        anchor="ls", stroke_width=st, stroke_fill=(20, 36, 84))
                if visible:
                    self.buttons[name] = ((vx0, top, vx1, top + h), (lambda mm=m: self.toggle_map(mm)))
                alt = False
            else:
                e = data
                ld.rectangle([0, y, vw, y + h - 1], fill=LIST_ALT if alt else LIST_BG)
                ld.line([0, y + h - 1, vw, y + h - 1], fill=(206, 218, 234))
                alt = not alt
                t0, t1 = time.localtime(e["start"]), time.localtime(e["end"])
                when = time.strftime("%b %d  %H:%M", t0) + " - " + time.strftime("%H:%M", t1)
                why = {"stopped": "stopped", "1 hour": "hourly save", "game closed": "game closed",
                       "quit": "quit", "interrupted": "interrupted", "character switched": "switched character"
                       }.get(e["reason"], e["reason"])
                line1 = f"{when}   ·   {fmt_dur(e['active_s'])}   ·   {why}"
                ld.text((u(18), y + u(13)), line1, font=font("bold", u(8.2)), fill=INK, anchor="ls")
                f2 = font("reg", u(8))
                runs = []
                if "exp" in e:
                    b = f"EXP {fmt_int(e['exp'])}"
                    if e.get("exp_h"):
                        b += f" ({fmt_short(e['exp_h'])}/h" + (f", {e['lvl_pct_h']:.1f}%/h)" if e.get("lvl_pct_h") else ")")
                    if e.get("levels"):
                        b += f" +{e['levels']} LV"
                    runs.append((b, f2, (52, 66, 92)))
                if "meso" in e:
                    b = f"Mesos {fmt_int(e['meso'])}"
                    if e.get("meso_h"):
                        b += f" ({fmt_short(e['meso_h'])}/h)"
                    runs.append((b, f2, (52, 66, 92)))
                if "loot_value" in e:
                    n_items = sum(r["qty"] for r in e.get("loot", []))
                    runs.append((f"Loot +{fmt_int(e['loot_value'])} ({n_items} items)", f2, (40, 110, 50)))
                if "pot_cost" in e:
                    used = sum(p["used"] for p in e.get("potions", {}).values())
                    runs.append((f"Potions -{fmt_int(e['pot_cost'])} ({used} used)", f2, (150, 60, 50)))
                if "profit" in e:
                    runs.append((f"Profit {profit_text(e['profit'])}", font("bold", u(8)),
                                 good if e["profit"] >= 0 else bad))
                x, ly, first = u(18), y + u(25), True
                for r in runs:
                    sep = 0 if first else ld.textlength("  ·  ", font=f2)
                    if not first and x + sep + ld.textlength(r[0], font=r[1]) > vw - u(30):
                        if ly >= y + h - u(8):           # out of lines
                            text_runs(ld, x, ly, [("  ...", f2, (120, 130, 150))], 0)
                            break
                        x, ly, first = u(18), ly + u(11), True     # next line
                    if not first:
                        x = text_runs(ld, x, ly, [("  ·  ", f2, (120, 130, 150))], 0)
                    x = text_runs(ld, x, ly, [r], 0)
                    first = False
                if visible:
                    self._del_boxes.append(((vx1 - u(26), top + u(8), vx1 - u(8), top + u(23)), e["id"]))
            y += h
        img.alpha_composite(layer.crop((0, self.scroll, vw, self.scroll + vh)), (vx0, vy0))
        for dbox, lid in self._del_boxes:
            self.button(img, f"del:{lid}", dbox, BTN_RED, "X", (lambda i=lid: app.ask_delete_log(i)))
        d = ImageDraw.Draw(img)
        sx0, sx1 = lx1 - u(6), lx1 - 1
        d.rectangle([sx0, ly0 + 1, sx1, ly1 - 1], fill=(214, 224, 236))
        if total > vh:
            th = max(u(14), int(vh * vh / total))
            ty0 = ly0 + 1 + int((vh - th) * self.scroll / max(1, total - vh))
            grad_rrect(img, (sx0, ty0, sx1 + 1, ty0 + th), u(2), (90, 150, 230), (40, 96, 190), outline=NAVY)

    def close(self):
        if self.closed:
            return
        self.map_suggest.close()
        try:
            self.app.cfg["controls_geometry"] = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
            save_json(CONFIG_PATH, self.app.cfg)
        except tk.TclError:
            pass
        super().close()
        if self.app.controls is self:
            self.app.controls = None


class CatalogWindow(MapleWindow):
    """Pick a potion from the common ones (MeowDB names, icons and NPC prices), or a custom item."""
    W, H = 384, 276
    TITLE = "ADD POTION"
    COLS = 5

    def __init__(self, app):
        super().__init__(app)
        self.query = ""
        # search box: the list narrows as you type; Enter adds the first match
        self.ent, self.var, _ = self.entry((self.u(58), self.u(30), self.Wp - self.u(12), self.u(46)), "",
                                           on_change=self.search, max_len=30)
        self.ent.bind("<Return>", lambda e: self.pick_first())
        par = app.controls.win if app.controls and not app.controls.closed else None
        self.centre_on(par)
        self.redraw()
        self.win.focus_force()
        self.ent.focus_set()

    def search(self, text):
        self.query = text.strip().lower()
        self.redraw()

    def matches(self):
        q = self.query
        if not q:
            return list(CATALOG)
        words = q.split()
        hits = [c for c in CATALOG if all(w in (c[1] + " " + c[3]).lower() for w in words)]
        return sorted(hits, key=lambda c: (not c[1].lower().startswith(q), c[1]))

    def pick_first(self):
        m = self.matches()
        if m:
            self.pick(m[0])

    def draw_body(self, img):
        W, H, u = self.Wp, self.Hp, self.u
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([u(4), u(25), W - u(5), H - u(5)], radius=u(3), fill=BODY_FILL, outline=(120, 150, 170))
        d.text((u(12), u(42)), "SEARCH", font=font("black", u(8.4)), fill=WHITE, anchor="ls",
               stroke_width=max(1, u(1)), stroke_fill=(38, 62, 96))
        items = self.matches()
        d.text((u(12), u(58)), f"{len(items)} match{'es' if len(items) != 1 else ''}" if self.query else
               "Pick what you drink (prices are NPC shop prices from MeowDB):",
               font=font("bold", u(8)), fill=(34, 56, 92), anchor="ls")
        if not items:
            d.text((W / 2, u(120)), "Nothing by that name here. Use CUSTOM ITEM to type in your own.",
                   font=font("bold", u(8.6)), fill=WHITE, anchor="ms", stroke_width=max(1, u(1)),
                   stroke_fill=(38, 62, 96))
        tw, th = (W - u(24) - u(4) * (self.COLS - 1)) / self.COLS, u(32)
        for i, (iid, name, price, kind) in enumerate(items):
            r, c = divmod(i, self.COLS)
            x0 = u(12) + c * (tw + u(4))
            y0 = u(63) + r * (th + u(3))
            key = f"item:{iid}"
            hov = self.hover == key
            d.rounded_rectangle([x0, y0, x0 + tw, y0 + th], radius=u(3),
                                fill=(255, 255, 255) if hov else LIST_BG, outline=NAVY if hov else (150, 170, 190))
            paste_icon(img, iid, x0 + u(3), y0 + u(5), u(22))
            d = ImageDraw.Draw(img)
            nf = font("bold", u(7.2))
            lines = wrap_text(d, name, nf, tw - u(29))
            if len(lines) > 2:
                lines = [lines[0], fit_text(d, " ".join(lines[1:]), nf, tw - u(29))]
            for j, line in enumerate(lines):
                d.text((x0 + u(27), y0 + u(10.5) + j * u(8.5)), fit_text(d, line, nf, tw - u(29)), font=nf,
                       fill=INK, anchor="ls")
            d.text((x0 + u(27), y0 + u(29)), (fmt_int(price) + " mesos") if price else "not sold",
                   font=font("reg", u(6.8)), fill=(80, 96, 124), anchor="ls")
            self.buttons[key] = ((int(x0), int(y0), int(x0 + tw), int(y0 + th)),
                                 (lambda e=(iid, name, price, kind): self.pick(e)))
        self.button(img, "custom", (u(12), H - u(26), u(140), H - u(10)), BTN_ORANGE, "CUSTOM ITEM...",
                    self.custom)
        self.button(img, "cancel", (W - u(12) - u(70), H - u(26), W - u(12), H - u(10)), BTN_GREY, "CANCEL",
                    self.close)

    def pick(self, e):
        iid, name, price, kind = e
        self.close()
        self.app.add_potion({"id": iid, "name": name, "price": price, "kind": kind, "slot": None})

    def custom(self):
        name = self.var.get().strip()
        self.close()
        self.app.ask_custom_potion(name)


# ----------------------------------------------------------------------------- app
class App:

    def __init__(self, cfg):
        self.cfg = cfg
        self.cfg.setdefault("controls_geometry", None)
        self.profiles = Profiles()
        if not self.profiles.names():
            self.profiles.create("Default")     # real characters get their own profile once seen on screen
        if not self.cfg.get("profile") or not self.profiles.exists(self.cfg["profile"]):
            self.cfg["profile"] = self.profiles.names()[0]
        self.profile = self.profiles.load(self.cfg["profile"])
        self.logs_version = 0
        self.session = Session(self.cfg.get("last_map", ""))
        self._recover()
        self.watcher = ExpWatcher()
        self.reader = ExpReader()
        self.count_reader = CountReader()
        self.counters = {}              # (potion name, slot) -> PotionCounter
        self.pot_now = {}               # potion index -> count read right now (for the POTIONS tab)
        self.ocr = Ocr()
        self.msgs = MessageReader(self.ocr)          # mesos (a narrow strip reads best)
        self.icon_ids = icon_ids()                   # item name -> icon, from what's cached
        self.names = ItemNames()                     # real item names (NiaDB's list, cached)
        self.items = ItemFeed(self.ocr, canon=self.canon_item)   # item pickups (a wider strip)
        self.prices = PriceBook()
        self.meow = MeowDB(self, online=bool(self.cfg.get("niadb_online")))
        global ICONS_ONLINE
        ICONS_ONLINE = bool(self.cfg.get("niadb_online"))
        self.import_pages()
        self.meow.item_index(lambda idx: self.names.set_names(idx.keys()) if idx else None)
        self.maps, self.map_id = [], None
        self.map_win = None
        self.meow.maps(lambda maps: setattr(self, "maps", maps or []))
        self.quit = False
        self.controls = None
        self.prompt = None
        self.catalog = None
        self.n = 0
        self._last_save = time.time()
        self._mono = time.monotonic()
        self._exp_seen = None           # last time the status bar read cleanly
        self._game_seen = None
        self.regions = {}               # screen areas being read: name -> (x0, y0, x1, y1)
        self._name_cand, self._name_n = None, 0
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self)
        if self.reader.tpl is None:
            log("statusbar_glyphs.npz missing - EXP can't be read")
        if self.ocr.error:
            log(self.ocr.error)

    def _recover(self):
        """A session still running when MapleTracker last stopped (crash, power cut): log it."""
        st = load_json(STATE_PATH, {})
        if not isinstance(st.get("session"), dict):
            return
        old = Session.from_json(st["session"])
        if old.state != STOPPED and old.loggable() and st.get("profile") and self.profiles.exists(st["profile"]):
            data = self.profiles.load(st["profile"])
            entry = old.log_entry("interrupted", st.get("need"))
            entry["end"] = st.get("saved_at", time.time())
            data["logs"].append(entry)
            self.profiles.save(data)
            if data["name"] == self.profile["name"]:
                self.profile = data
            log(f"logged an interrupted session at {old.map} for {data['name']}")

    def open_controls(self):
        if self.controls is not None and not self.controls.closed:
            try:
                self.controls.win.deiconify()
                self.controls.win.lift()
                self.controls.win.focus_force()
                return
            except tk.TclError:
                pass
        self.controls = TrackerWindow(self)
        self.render(time.time())

    def ask(self, *a, **kw):
        if self.prompt is not None:
            self.prompt.close()
        kw.setdefault("parent", self.controls.win if self.controls and not self.controls.closed else None)
        self.prompt = PromptWindow(self, *a, **kw)

    # --- the session
    def set_map(self, text):
        self.session.map = text
        self.cfg["last_map"] = text

    def start(self):
        se = self.session
        if se.state == STOPPED:
            se.clear()
            se.started = time.time()
            log(f"started{(' at ' + se.map.strip()) if se.map.strip() else ''}")
        elif se.state == PAUSED:
            log("resumed")
        se.state = RUNNING
        self.after_change()

    def pause(self):
        if self.session.state == RUNNING:
            self.session.state = PAUSED
            log("paused")
            self.after_change()

    def toggle(self):
        if self.session.state == RUNNING:
            self.pause()
        else:
            self.start()

    def stop(self, reason="stopped"):
        """End the session; it's logged if it has a map name and lasted a minute."""
        se = self.session
        if se.state == STOPPED:
            return
        self._log_session(reason)
        se.state = STOPPED
        log(f"stopped ({reason})")
        save_json(CONFIG_PATH, self.cfg)
        self.after_change()

    def _log_session(self, reason):
        se = self.session
        if not se.loggable():
            return None
        entry = se.log_entry(reason, self.watcher.need, self.cfg["trackers"], self.loot_summary())
        self.prices.save()
        self.profile["logs"].append(entry)
        self.profiles.save(self.profile)
        self.logs_version += 1
        log(f"logged {fmt_dur(entry['active_s'])} at {entry['map']} for {self.profile['name']}: "
            f"{fmt_int(entry.get('exp', 0))} EXP, {fmt_int(entry.get('meso', 0))} mesos, "
            f"potions {fmt_int(entry.get('pot_cost', 0))}")
        return entry

    def reset(self, which):
        if which in ("exp", "both", "all"):
            self.session.reset_exp()
        if which in ("meso", "mesoloot", "both", "all"):
            self.session.reset_meso()
        if which in ("loot", "mesoloot", "all"):
            self.session.reset_loot()
        if which in ("potions", "all"):
            self.session.reset_potions()
        log(f"reset {which}")
        self.after_change()

    def after_change(self):
        self.save_state()
        self.render(time.time())

    def save_state(self):
        try:
            save_json(STATE_PATH, {"session": self.session.to_json(), "profile": self.profile["name"],
                                   "need": self.watcher.need, "saved_at": time.time()})
        except OSError as ex:
            log("could not save state:", ex)

    # --- characters and logs
    def profile_menu(self, x, y):
        m = tk.Menu(self.root, tearoff=0, bg="#eef4fb", fg="#1d2b45", activebackground="#4f7fd0",
                    activeforeground="#ffffff", font=("Arial", -int(15 * self.controls.k / 1.69), "bold"),
                    relief="solid", bd=1)
        cur = tk.StringVar(value=self.profile["name"])
        self._menu_var = cur
        for name in self.profiles.names():
            m.add_radiobutton(label=name, variable=cur, value=name,
                              command=lambda n=name: self.switch_profile(n))
        m.add_separator()
        m.add_command(label="New character...", command=self.ask_new_profile)
        m.add_command(label=f"Rename {self.profile['name']}...", command=self.ask_rename_profile)
        m.add_command(label=f"Delete {self.profile['name']}...", command=self.ask_delete_profile,
                      state="normal" if len(self.profiles.names()) > 1 else "disabled")
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def switch_profile(self, name):
        if name == self.profile["name"]:
            return
        self.stop("character switched")
        self.profile = self.profiles.load(name)
        self.cfg["profile"] = name
        save_json(CONFIG_PATH, self.cfg)
        self.logs_version += 1
        log(f"character: {name}")
        self.after_change()

    def _check_name(self, text, allow=None):
        name = Profiles.clean(text)
        if not name:
            return None, "Type a name."
        if self.profiles.exists(name) and name.lower() != (allow or "").lower():
            return None, f"There's already a character called {name}."
        return name, None

    def ask_new_profile(self):
        def ok(text):
            name, err = self._check_name(text)
            if err:
                return err
            self.profiles.create(name)
            self.switch_profile(name)
        self.ask("New character", "Name of the character (each one keeps its own log):", ok, initial="",
                 ok_text="CREATE")

    def ask_rename_profile(self):
        old = self.profile["name"]

        def ok(text):
            name, err = self._check_name(text, allow=old)
            if err:
                return err
            self.profiles.rename(old, name)
            self.profile = self.profiles.load(name)
            self.cfg["profile"] = name
            save_json(CONFIG_PATH, self.cfg)
            self.logs_version += 1
            self.after_change()
        self.ask("Rename character", f"New name for {old}:", ok, initial=old, ok_text="RENAME")

    def ask_delete_profile(self):
        name = self.profile["name"]
        n = len(self.profile["logs"])

        def ok(_):
            self.stop("character switched")
            self.profiles.delete(name)
            rest = self.profiles.names()
            self.profile = self.profiles.load(rest[0])
            self.cfg["profile"] = rest[0]
            save_json(CONFIG_PATH, self.cfg)
            self.logs_version += 1
            log(f"deleted character {name}")
            self.after_change()
        self.ask("Delete character", f"Delete {name} and their {n} log{'s' if n != 1 else ''}? "
                 "This can't be undone.", ok, ok_text="DELETE", ok_pal=BTN_RED)

    def ask_delete_log(self, lid):
        e = next((x for x in self.profile["logs"] if x["id"] == lid), None)
        if e is None:
            return
        when = time.strftime("%b %d %H:%M", time.localtime(e["start"]))

        def ok(_):
            self.profile["logs"] = [x for x in self.profile["logs"] if x["id"] != lid]
            self.profiles.save(self.profile)
            self.logs_version += 1
            self.render(time.time())
        self.ask("Delete log", f"Delete the {fmt_dur(e['active_s'])} session at {e['map']} from {when}?",
                 ok, ok_text="DELETE", ok_pal=BTN_RED)

    def ask_clear_logs(self):
        n = len(self.profile["logs"])

        def ok(_):
            self.profile["logs"] = []
            self.profiles.save(self.profile)
            self.logs_version += 1
            log(f"cleared {n} logs for {self.profile['name']}")
            self.render(time.time())
        self.ask("Clear all logs", f"Delete all {n} of {self.profile['name']}'s logs? This can't be undone.",
                 ok, ok_text="CLEAR ALL", ok_pal=BTN_RED)

    # --- settings
    def set_tracker(self, key, on):
        self.cfg["trackers"][key] = bool(on)
        save_json(CONFIG_PATH, self.cfg)
        if key == "potions":
            self.counters = {}
        log(f"{key} tracker {'on' if on else 'off'}")
        self.render(time.time())

    def set_profit(self, key, on):
        self.cfg["profit"][key] = bool(on)
        save_json(CONFIG_PATH, self.cfg)
        self.render(time.time())

    def ask_item_price(self, name):
        unit, src = self.prices.unit(name)
        e = self.prices.items.get(name) or {}

        def ok(text):
            text = (text or "").strip().lower()
            if text in ("", "npc", "-"):
                self.prices.set_price(name, None)            # back to the NPC / default price
            else:
                v = parse_mesos(text)
                if v is None:
                    return "Type a price like 2500, 13k or 1.55m."
                self.prices.set_price(name, v)
            self.logs_version += 1
            self.render(time.time())
        npc = e.get("npc")
        hint = f" NPC buys it for {fmt_int(npc)}." if npc is not None else ""
        self.ask("Item price", f"What is one {name} worth to you? e.g. 2500, 13k or 1.55m - empty for the NPC price.{hint}",
                 ok, initial="" if e.get("price") is None else str(e["price"]), ok_text="SAVE")

    def toggle_item(self, name):
        e = self.prices.entry(name)
        self.prices.set_include(name, not e.get("include", True))
        self.logs_version += 1
        self.render(time.time())

    def set_value(self, key, value, save=True):
        """A setting that isn't on/off (EXP display, underlay style and opacity)."""
        self.cfg[key] = value
        if save:
            save_json(CONFIG_PATH, self.cfg)
        self.render(time.time())

    def set_option(self, key, on):
        self.cfg[key] = bool(on)
        save_json(CONFIG_PATH, self.cfg)
        if key == "hide_from_screenshots":
            for w in (self.overlay, self.controls, self.prompt, self.catalog):
                if w is not None and not getattr(w, "closed", False):
                    try:
                        user32.SetWindowDisplayAffinity(w.hwnd, 0x11 if on else 0)
                    except Exception:
                        pass
            try:
                user32.SetWindowDisplayAffinity(self.overlay.uhwnd, 0x11 if on else 0)
            except Exception:
                pass
        self.render(time.time())

    # --- potions
    def _potions_changed(self):
        self.profiles.save(self.profile)
        self.counters = {}
        self.pot_now = {}
        self.render(time.time())

    def open_catalog(self):
        if self.catalog is not None and not self.catalog.closed:
            self.catalog.close()
        self.catalog = CatalogWindow(self)

    def add_potion(self, p):
        if any(x["name"].lower() == p["name"].lower() for x in self.profile["potions"]):
            p["name"] += " (2)"
        self.profile["potions"].append(p)
        i = len(self.profile["potions"]) - 1
        # if its icon is clearly on one of the quick slots (and that slot is free), put it there
        taken = {tuple(x["slot"]) for x in self.profile["potions"] if x.get("slot")}
        found = self.find_potion_slot(p.get("id"), self.capture_quickslots()) if p.get("id") else None
        if found and found not in taken:
            p["slot"] = list(found)
            log(f"{p['name']}: found it on quick slot {slot_name(found)}")
        self._potions_changed()
        if self.controls and not self.controls.closed:
            self.controls.set_tab("potions")
        if p.get("price") is None:
            self.ask_potion_price(i)

    def ask_custom_potion(self, name=""):
        def ok(text):
            name = (text or "").strip()[:28]
            if not name:
                return "Type the item's name."
            self.root.after(50, lambda: self.add_potion({"id": None, "name": name, "price": None, "kind": "",
                                                         "slot": None}))
        self.ask("Custom item", "Name of the potion or food:", ok, initial=name, ok_text="NEXT")

    def ask_potion_price(self, i):
        p = self.profile["potions"][i]

        def ok(text):
            v = parse_mesos(text)
            if v is None:
                return "Type a price like 80, 1.5k or 2m."
            if v > 100_000_000:
                return "That price doesn't look right."
            p["price"] = v
            self._potions_changed()
        self.ask("Potion price", f"What does one {p['name']} cost you (mesos)?", ok,
                 initial="" if p.get("price") is None else str(p["price"]), ok_text="SAVE")

    def set_potion_slot(self, i, slot):
        """One potion per slot: whatever was on that slot comes off it."""
        for j, p in enumerate(self.profile["potions"]):
            if j != i and p.get("slot") and tuple(p["slot"]) == tuple(slot):
                p["slot"] = None
        self.profile["potions"][i]["slot"] = list(slot)
        log(f"{self.profile['potions'][i]['name']} is on quick slot {slot_name(slot)}")
        self._potions_changed()

    def clear_potion_slot(self, i):
        self.profile["potions"][i]["slot"] = None
        self._potions_changed()

    def remove_potion(self, i):
        del self.profile["potions"][i]
        self._potions_changed()

    def capture_quickslots(self):
        """A picture of the game's quick slots and each slot's box in it, or None (game not on
        screen, or one of our own windows is in front of the quick slots)."""
        hwnd = find_game_window()
        if not hwnd or user32.IsIconic(hwnd):
            return None
        rect = client_rect(hwnd)
        if rect[2] <= 0 or rect[3] <= 0:
            return None
        s = rect[3] / self.cfg["game_base_height"]
        x0, y0 = slot_origin(0, 0, rect, s, self.cfg)
        x1, y1 = slot_origin(3, 2, rect, s, self.cfg)
        m = 7 * s
        X0, Y0, X1, Y1 = int(x0 - m), int(y0 - m), int(x1 + 28 * s + m), int(y1 + 31 * s + m)
        for w in (self.controls, self.prompt, self.catalog):
            if w is not None and not w.closed and not self.cfg.get("hide_from_screenshots"):
                try:
                    box = (w.win.winfo_rootx(), w.win.winfo_rooty(), w.win.winfo_rootx() + w.win.winfo_width(),
                           w.win.winfo_rooty() + w.win.winfo_height())
                except tk.TclError:
                    continue
                if overlaps(box, (X0, Y0, X1, Y1)):
                    return None
        try:
            shot = self._grab(X0, Y0, X1 - X0, Y1 - Y0)
        except Exception:
            return None
        boxes = {}
        for r in range(3):
            for c in range(4):
                sx, sy = slot_origin(c, r, rect, s, self.cfg)
                boxes[(c, r)] = (sx - X0 - 3 * s, sy - Y0 - 3 * s, sx - X0 + 30 * s, sy - Y0 + 28 * s)
        img = Image.fromarray(cv2.cvtColor(shot, cv2.COLOR_BGR2RGB))
        return img, boxes

    @staticmethod
    def find_potion_slot(item_id, panel):
        """The slot whose picture best matches the item's icon, if one clearly does."""
        icon = item_icon(item_id)
        if icon is None or panel is None:
            return None
        pimg, boxes = panel
        arr = cv2.cvtColor(np.asarray(pimg.convert("RGB")), cv2.COLOR_RGB2BGR)
        scores = {}
        for slot, (x0, y0, x1, y1) in boxes.items():
            cell = arr[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)]
            best = 0.0
            for frac in (0.70, 0.80, 0.90):
                size = int(round(min(cell.shape[0], cell.shape[1]) * frac))
                if size < 8:
                    continue
                ic = np.asarray(icon.resize((size, size), Image.LANCZOS))
                tpl = cv2.cvtColor(np.ascontiguousarray(ic[:, :, :3]), cv2.COLOR_RGB2BGR)
                alpha = (ic[:, :, 3] > 128).astype(np.uint8) * 255
                res = cv2.matchTemplate(cell, tpl, cv2.TM_CCOEFF_NORMED, mask=cv2.merge([alpha] * 3))
                res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
                best = max(best, float(res.max()) if res.size else 0.0)
            scores[slot] = best
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        (slot, v), v2 = ranked[0], ranked[1][1] if len(ranked) > 1 else 0.0
        return slot if v >= 0.55 and v - v2 >= 0.12 else None

    def scan_potions(self, rect, s, now):
        pots = self.profile.get("potions", [])
        regions = []
        for i, p in enumerate(pots):
            slot = p.get("slot")
            if not slot:
                continue
            # the count's corner, read on the game's own pixel grid
            nx0, ny0, sx, sy = CountReader.corner(slot, rect, self.cfg, s)
            x0, y0, x1, y1 = CountReader.grab_box(nx0, ny0, sx, sy)
            regions.append((rect[0] + x0, rect[1] + y0, rect[0] + x1, rect[1] + y1))
            patch = self._grab(rect[0] + x0, rect[1] + y0, x1 - x0, y1 - y0)
            ctr = self.counters.setdefault((p["name"], tuple(slot)), PotionCounter())
            value = self.count_reader.read(CountReader.native(patch, x0, y0, nx0, ny0, sx, sy), near=ctr.last)
            if value is not None:
                self.pot_now[i] = value
            used, note = ctr.update(value, now)
            if used:
                self.session.add_potions(p["name"], p.get("id"), used, p.get("price"))
            if (used or note) and (self.cfg["debug"] or (note and "not counted" in note)):
                log(f"{p['name']}: {value}" + (f", {used} used" if used else "") + (f" ({note})" if note else "")
                    + ("" if self.session.running or not used else " (not running: not counted)"))
        if regions:
            self.regions["your potion slots"] = (min(r[0] for r in regions), min(r[1] for r in regions),
                                                 max(r[2] for r in regions), max(r[3] for r in regions))
        else:
            self.regions.pop("your potion slots", None)

    def shutdown(self, reason="quit"):
        if self.quit:
            return
        self.stop(reason)
        self.quit = True
        self.save_state()
        save_json(CONFIG_PATH, self.cfg)
        for w in (self.prompt, self.catalog, self.map_win, self.controls):
            if w is not None:
                w.close()
        self.root.destroy()

    # --- reading the game
    def _grab(self, x, y, w, h):
        return np.ascontiguousarray(np.asarray(
            self.sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)}))[:, :, :3])

    def scan_exp(self, rect, s, now):
        cx, cy, cw, ch = rect
        e = self.cfg["exp_text"]
        mid = cx + cw / 2
        x0, x1 = mid + e["dx0"] * s, mid + e["dx1"] * s
        y0, y1 = cy + ch - e["dy0"] * s, cy + ch - e["dy1"] * s
        self.regions["the EXP number"] = (x0, y0, x1, y1)
        reading = self.reader.read(self._grab(x0, y0, x1 - x0, y1 - y0), s)
        if reading is None:
            return
        self._exp_seen = now
        gain, levels, what = self.watcher.update(reading, now)
        if self.cfg["trackers"].get("exp"):
            self.session.add_exp(gain, levels)
        if what and (self.cfg["debug"] or "level" in what or "lost" in what):
            log(f"EXP {reading[0]:,} [{reading[1]:.2f}%]: {what}"
                + ("" if self.session.running else " (not running: not counted)"))

    def scan_messages(self, rect, s, now):
        cx, cy, cw, ch = rect
        a = self.cfg["msg_area"]
        x0, y0, y1 = cx + cw - a["width"] * s, cy + ch - a["top"] * s, cy + ch - a["bottom"] * s
        self.regions["the pickup messages"] = (x0, y0, cx + cw, y1)
        shot = self._grab(x0, y0, cx + cw - x0, y1 - y0)
        if self.cfg["trackers"].get("loot"):
            wx0 = cx + cw - self.cfg["item_area_width"] * s
            for ev in self.items.update(self._grab(wx0, y0, cx + cw - wx0, y1 - y0), s, now):
                if ev[0] != "item" or not ev[1]:
                    continue
                _, name, qty, cat = ev
                self.prices.seen(name, cat or None)
                self.session.add_loot(name, qty, cat or None)
                if self.cfg["debug"]:
                    log(f"picked up {name} x{qty}" + (f" ({cat})" if cat else "")
                        + ("" if self.session.running else " (not running: not counted)"))
        for ev in self.msgs.update(shot, s, now):
            if ev[0] != "meso":
                continue
            amt = ev[1]
            if not self.cfg["trackers"].get("meso"):
                continue
            self.session.add_meso(amt)
            if self.cfg["debug"]:
                log(f"mesos +{amt:,}" + ("" if self.session.running else " (not running: not counted)"))

    def current_map(self):
        """The map you're on, from NiaDB's map list - matched by the name in the MAP box."""
        name = self.session.map.strip().lower()
        if not name or not self.maps:
            return None
        hits = [m for m in self.maps if m["name"].strip().lower() == name]
        if self.map_id:
            hits = [m for m in hits if m["id"] == self.map_id] or hits
        return hits[0] if hits else None

    def import_pages(self):
        """Read pages saved in meowdb_import/ into the cache (and NPC prices into the price book)."""
        n = 0
        self.icon_ids = getattr(self, "icon_ids", None) or icon_ids()
        for kind, oid, name, info in import_saved_pages():
            n += 1
            if kind == "item-db" and info.get("name") and info.get("npc") is not None:
                self.prices.entry(info["name"])["npc"] = info["npc"]
        if n:
            self.prices.save()
            self.icon_ids = icon_ids()
        return n

    def open_map_info(self):
        m = self.current_map()
        if m is None:
            return
        if self.map_win is not None and not self.map_win.closed:
            self.map_win.close()
        self.map_win = MapInfoWindow(self, m)

    def likely_items(self):
        """Names of items you're likely to pick up: what the current map's monsters drop (from the
        cached NiaDB pages), your potions and what you've looted before."""
        m = self.current_map()
        key = (m["id"] if m else None, len(self.prices.items))
        if getattr(self, "_likely_key", None) == key:
            return self._likely
        names = {p["name"] for p in self.profile.get("potions", [])} | set(self.prices.items)
        names |= {n for _, n, *_ in CATALOG}
        if m:
            for mob in load_json(MeowDB._path(f"map_{m['id']}"), []) or []:
                for dr in load_json(MeowDB._path(f"mob_{mob['id']}"), []) or []:
                    if isinstance(dr, dict) and dr.get("name"):
                        names.add(dr["name"])
        self._likely_key, self._likely = key, names
        return names

    def canon_item(self, raw):
        """The real name of an item read off the screen; None for OCR junk (when the item list is
        known, a name that matches nothing in it isn't counted). A rare item that only looks like
        the read beats nothing it resembles: "Red Pao" (an overall) next to "Red Potion" on a map
        whose monsters drop Red Potions is a Red Potion."""
        import difflib
        name = self.names.canon(raw)
        if self.names.names and name not in self.names.lower.values():
            name = None
        likely = self.likely_items()
        if name in likely:
            return name
        base = raw[:-3].strip() if raw.endswith("...") else raw
        bl = base.lower()
        best, score = None, 0.0
        for n in likely:
            nl = n.lower()
            r = difflib.SequenceMatcher(None, bl, nl[:len(bl)] if raw.endswith("...") else nl).ratio()
            if r > score:
                best, score = n, r
        if best and score >= (0.7 if name else 0.8):
            return best
        return name

    def loot_summary(self):
        """(value of this session's loot that counts toward profit, [rows]) - priced right now."""
        pots = {p["name"].lower(): p for p in self.profile.get("potions", [])}
        prof = self.cfg["profit"]
        total, rows = 0, []
        for name, e in sorted(self.session.loot.items()):
            unit, src = self.prices.unit(name)
            kind = loot_kind(name, e.get("cat"))
            entry = self.prices.items.get(name) or {}
            include = entry.get("include", True) and prof.get(kind, True)
            pot = pots.get(name.lower())
            if pot and prof.get("free_potions", True):
                # your own potion, looted: worth what you'd pay for it, so drinking it costs nothing
                unit, src, kind, include = pot.get("price") or 0, "potion", "potion", True
            value = e["qty"] * unit
            if include:
                total += value
            rows.append({"name": name, "qty": e["qty"], "unit": unit, "src": src, "kind": kind,
                         "value": value, "included": include})
        return total, rows

    def scan_name(self, rect, s):
        """Read the character's name off the status bar; switch to that character's profile once
        the same new name has been read a few times in a row (one misread never switches)."""
        if self.ocr.engine is None:
            return
        cx, cy, cw, ch = rect
        a = self.cfg["name_text"]
        mid = cx + cw / 2
        x0, x1 = mid + a["dx0"] * s, mid + a["dx1"] * s
        y0, y1 = cy + ch - a["dy0"] * s, cy + ch - a["dy1"] * s
        self.regions["your character's name"] = (x0, y0, x1, y1)
        g = cv2.cvtColor(self._grab(x0, y0, x1 - x0, y1 - y0), cv2.COLOR_BGR2GRAY)
        up = max(1.0, 1.875 / s)
        if up > 1.0:
            g = cv2.resize(g, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
        g = cv2.copyMakeBorder(g, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=int(np.median(g)))
        lines = [ln[0].strip() for ln in self.ocr.lines(g) if ln[0].strip()]
        if len(lines) < 2:
            return                          # the class and the name are both there when readable
        name = re.sub(r"[^A-Za-z0-9]", "", lines[-1])
        if not 2 <= len(name) <= 13:
            return
        if self._match_profile(name) == self.profile["name"]:
            self._name_cand, self._name_n = None, 0
            return
        if name != self._name_cand:
            self._name_cand, self._name_n = name, 1
            return
        self._name_n += 1
        known = self._match_profile(name) is not None
        if self._name_n < (3 if known else 5):    # a brand-new character: be extra sure
            return
        self._name_cand, self._name_n = None, 0
        target = self._match_profile(name)
        if target is None:
            target = Profiles.clean(name)
            self.profiles.create(target)
            log(f"new character on screen: {target} (made a profile for them)")
        else:
            log(f"character on screen: {target}")
        self.switch_profile(target)

    @staticmethod
    def _name_key(name):
        """A name with the letters OCR mixes up made equal (I/l/1/i, O/0, cl/d, rn/m), so a slip
        still finds the right character - but Maple and Maple2 stay different characters."""
        k = name.lower().replace("cl", "d").replace("rn", "m")
        return k.translate(str.maketrans("0i1|", "olll"))

    def _match_profile(self, name):
        """The profile for a name read off the screen, or None."""
        key = self._name_key(name)
        for n in self.profiles.names():
            if self._name_key(n) == key:
                return n
        return None

    def _covering(self):
        """Our windows show up in screenshots (unless hide_from_screenshots), so warn when one
        sits on an area being read - it would hide what's underneath."""
        if self.cfg.get("hide_from_screenshots"):
            return []
        wins = [("Overlay", self.overlay.win)]
        if self.controls and not self.controls.closed:
            wins.append(("Tracker window", self.controls.win))
        out = []
        for name, w in wins:
            try:
                if not w.winfo_viewable():
                    continue
                box = (w.winfo_rootx(), w.winfo_rooty(),
                       w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height())
            except tk.TclError:
                continue
            for what, reg in self.regions.items():
                if overlaps(box, reg):
                    out.append(f"{name} is covering {what} - move it")
        return out

    # --- display
    def render(self, now):
        se = self.session
        er, mr = per_hour(se.exp, se.exp_t), per_hour(se.meso, se.meso_t)
        need, last = self.watcher.need, self.watcher.last
        ttl = (need - last[0]) / er * 3600 if (er and need and last) else None
        s = {
            # overlay
            "state": se.state, "map": se.map.strip(), "profile": self.profile["name"],
            "exp": fmt_int(se.exp), "exph": (fmt_short(er) + "/h") if er is not None else "-/h",
            # the same as a share of this level's EXP bar
            "exppct": f"{se.exp / need * 100:.2f}%" if need else "-%",
            "pcth": f"{er / need * 100:.2f}%/h" if (er and need) else "-%/h",
            "exp_display": self.cfg.get("exp_display", "exp"),
            "under": (self.cfg.get("underlay", "off"), round(float(self.cfg.get("underlay_opacity", 0.55)), 2)),
            "pct": last[1] if last else None, "ttl": fmt_dur(ttl) if ttl is not None else None,
            "meso": fmt_int(se.meso), "mesoh": (fmt_short(mr) + "/h") if mr is not None else "-/h",
            "elapsed": fmt_dur(se.active),
            "hover_btn": getattr(self.overlay, "hover_btn", None),
            # tracker window
            # (with "both", the card's last row already shows level % / hour)
            "c_exp_total": (f"{se.exp / need * 100:.2f}%" if need else "-")
                           if self.cfg.get("exp_display") == "pct" else fmt_int(se.exp),
            "c_exp_rate": ((f"{er / need * 100:.2f}% /h" if need else "-")
                           if self.cfg.get("exp_display") == "pct" else fmt_int(er) + " /h")
                          if er is not None else "-",
            "c_exp_time": fmt_dur(se.exp_t),
            "c_exp_extra_label": "Next level in", "c_exp_extra": fmt_dur(ttl) if ttl is not None else "-",
            "c_exp_extra2_label": "Levels gained" if se.levels else "Level % / hour",
            "c_exp_extra2": str(se.levels) if se.levels else
                            (f"{er / need * 100:.1f}%" if (er and need) else "-"),
            "c_meso_total": fmt_int(se.meso), "c_meso_rate": (fmt_int(mr) + " /h") if mr is not None else "-",
            "c_meso_time": fmt_dur(se.meso_t),
            "c_meso_extra_label": "Pickups", "c_meso_extra": fmt_int(se.pickups),
            "c_meso_extra2_label": "Average", "c_meso_extra2": fmt_int(se.meso / se.pickups) if se.pickups else "-",
        }
        on = self.cfg["trackers"]
        pr = per_hour(se.pot_cost, se.pot_t)
        loot_v, loot_rows = self.loot_summary()
        lh = per_hour(loot_v, se.loot_t)
        profit = (se.meso if on.get("meso") else 0) + (loot_v if on.get("loot") else 0) - \
            (se.pot_cost if on.get("potions") else 0)
        profit_h = per_hour(profit, se.active)
        n_items = sum(r["qty"] for r in loot_rows)
        s.update({
            "loot": ("+" if loot_v >= 0 else "") + fmt_int(loot_v), "looth": (fmt_short(lh) + "/h") if lh is not None else "-/h",
            "lootx": "" if self.cfg.get("overlay_loot_open") else f"{n_items} item{'s' if n_items != 1 else ''}",
            "c_loot_total": fmt_int(loot_v), "c_loot_items": fmt_int(n_items),
            "loot_open": bool(self.cfg.get("overlay_loot_open")),
            "loot_grid": tuple((getattr(self, "icon_ids", {}).get(r["name"].lower()), r["name"], r["qty"])
                               for r in sorted(loot_rows, key=lambda r: -r["qty"])),
            "on": dict(on), "pots_setup": bool(self.profile.get("potions")),
            "pots": [(p.get("id"), name, p["used"]) for name, p in se.pots.items() if p["used"]],
            "potcost": fmt_int(se.pot_cost), "poth": (fmt_short(pr) + "/h") if pr is not None else "-/h",
            "c_pot_rate": ("-" + fmt_int(pr) + " /h") if pr else "-",
            "profit_v": profit, "profit": profit_text(profit),
            "profith": ((("+" if profit_h >= 0 else "-") + fmt_short(abs(profit_h)) + "/h")
                        if profit_h is not None else "-/h"),
        })
        problems = []
        if self.reader.tpl is None:
            problems.append("EXP: statusbar_glyphs.npz is missing")
        elif se.running and on.get("exp") and (self._exp_seen is None or time.monotonic() - self._exp_seen > 20):
            problems.append("EXP: can't read the status bar")
        if self.ocr.error and on.get("meso"):
            problems.append("Mesos: " + self.ocr.error)
        if on.get("potions") and self.count_reader.tpl is None:
            problems.append("Potions: count_glyphs.npz is missing")
        elif on.get("potions") and any(not p.get("slot") for p in self.profile.get("potions", [])):
            problems.append("Potions: drag each potion onto its quick slot (POTIONS tab)")
        problems += self._covering()
        s["status"] = "\n".join(problems)
        self.overlay.render(s)
        if self.controls and not self.controls.closed:
            try:
                self.controls.render(s)
            except tk.TclError:
                self.controls = None

    # --- main loop
    def tick(self):
        if self.quit:
            return
        mono = time.monotonic()
        dt, self._mono = min(5.0, mono - self._mono), mono
        if self.n % 5 == 0 and os.path.exists(OPEN_FLAG):
            try:
                os.remove(OPEN_FLAG)
            except OSError:
                pass
            self.open_controls()
        hwnd = find_game_window()
        if hwnd:
            self._game_seen = mono
        elif self._game_seen is not None and self.cfg["close_with_game"] and \
                mono - self._game_seen > self.cfg["close_after_s"]:
            if game_process_running():
                self._game_seen = mono
            else:
                log("MapleStory closed - closing MapleTracker")
                self.shutdown("game closed")
                return
        se = self.session
        se.advance(dt)
        self.meow.poll()
        if se.running and se.active >= SEGMENT_S:
            # an hour of tracking: log it and carry on with a fresh hour at the same map
            if self._log_session("1 hour"):
                se.clear()
                se.started = time.time()
            self.after_change()
        fg = user32.GetForegroundWindow()
        game_front = bool(hwnd) and fg == hwnd and not user32.IsIconic(hwnd)
        if hwnd and not self.overlay._placed:
            self.overlay.place(hwnd)
        if game_front:
            rect = client_rect(hwnd)
            s = rect[3] / self.cfg["game_base_height"]
            if rect[2] > 0 and rect[3] > 0:
                on = self.cfg["trackers"]
                try:
                    if on.get("meso"):
                        self.scan_messages(rect, s, mono)
                    else:
                        self.regions.pop("the pickup messages", None)
                    # the EXP number is always followed (it's cheap), so turning EXP on in the
                    # middle of a session never counts EXP gained while it was off
                    if self.n % 3 == 0:
                        self.scan_exp(rect, s, mono)
                    if on.get("potions") and self.n % 2 == 0:
                        self.scan_potions(rect, s, mono)
                    if self.cfg.get("auto_profile") and self.n % 14 == 7:
                        self.scan_name(rect, s)
                except Exception as ex:
                    log("scan error:", repr(ex))
        ours = {self.overlay.hwnd}
        for w in (self.controls, self.prompt, self.catalog, self.map_win):
            if w is not None and not w.closed:
                ours.add(w.hwnd)
        visible = not self.cfg["hide_when_game_not_focused"] or (bool(hwnd) and (fg == hwnd or fg in ours))
        self.overlay.set_visible(visible)
        if self.n % 3 == 0:
            self.render(time.time())
        if time.time() - self._last_save > 15:
            self._last_save = time.time()
            self.save_state()
        self.n += 1
        self.root.after(max(30, int(1000 / self.cfg["scan_hz"])), self.tick)

    def run(self):
        self.root.after(100, self.tick)
        if "--controls" in sys.argv:
            self.root.after(500, self.open_controls)
        self.root.mainloop()


# ----------------------------------------------------------------------------- offline checks
def replay(folder_bar, folder_msgs):
    """Run the readers over recorded captures (2560x1440 client, s = 1.875) and print results."""
    import glob
    s = 1440 / 768
    if folder_bar:
        r, t, gained = ExpReader(), ExpWatcher(), 0
        for f in sorted(glob.glob(os.path.join(folder_bar, "*.png"))):
            im = cv2.imread(f)
            # captures start at client x 930, y 1378; the reader's strip is relative to the centre
            mid, bot = 1280 - 930, 1440 - 1378
            e = DEFAULTS["exp_text"]
            strip = im[int(bot - e["dy0"] * s):int(bot - e["dy1"] * s), int(mid + e["dx0"] * s):int(mid + e["dx1"] * s)]
            rd = r.read(strip, s)
            what = None
            for _ in range(2):
                if rd:
                    g, _lv, w = t.update(rd)
                    gained += g
                    what = w or what
            print(os.path.basename(f), rd, what or "", f"gained={gained}")
    if folder_msgs:
        ocr = Ocr()
        mr = MessageReader(ocr)
        total = []
        for f in sorted(glob.glob(os.path.join(folder_msgs, "*.png"))):
            im = cv2.imread(f)
            # captures are the client region x 1408.., y 648.. (right 45%, bottom 55%)
            a = DEFAULTS["msg_area"]
            x0, y0, y1 = int(1152 - a["width"] * s), int(792 - a["top"] * s), int(792 - a["bottom"] * s)
            ts = float(os.path.basename(f)[:-4])
            got = [ev[1] for ev in mr.update(im[y0:y1, x0:], s, ts) if ev[0] == "meso"]
            if got:
                total += got
                print(os.path.basename(f), "mesos", got)
        print("meso messages counted:", total, "sum", sum(total))


def main():
    if "--replay" in sys.argv:
        i = sys.argv.index("--replay")
        bar = sys.argv[i + 1] if len(sys.argv) > i + 1 and sys.argv[i + 1] != "-" else None
        msgs = sys.argv[i + 2] if len(sys.argv) > i + 2 else None
        replay(bar, msgs)
        return
    # one MapleTracker at a time: starting it again just opens the running one's tracker window
    ctypes.windll.kernel32.CreateMutexW(None, False, "MapleTracker.single-instance")
    if ctypes.windll.kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        try:
            open(OPEN_FLAG, "w").close()
        except OSError:
            pass
        return
    cfg = load_config()
    save_json(CONFIG_PATH, cfg)
    if "--debug" in sys.argv:
        cfg["debug"] = True              # this run only (not saved)
    try:
        open(LOG_PATH, "w", encoding="utf-8").close()     # a fresh log each run
    except OSError:
        pass
    log("MapleTracker started")
    App(cfg).run()


if __name__ == "__main__":
    main()
