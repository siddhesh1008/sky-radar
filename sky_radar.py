#!/usr/bin/env python3
"""Sky Radar: live aircraft around you on a round 1080x1080 display.

Runs on Windows for development and on a Raspberry Pi 5 fullscreen.
Data comes from the adsb.lol API (source "api") or your own
RTL-SDR receiver running readsb/tar1090 (source "local").

Controls: drag to move around the map, pinch to zoom (touch screen), tap/click a plane for details, tap empty space to close,
crosshair button or H = back to your location (blue dot), +/- buttons or mouse wheel to zoom, MAP button or M = map on/off,
gear button or S = settings (location search, Wi-Fi), F = fullscreen, Esc/Q = quit.

First start: if there is no internet it opens Wi-Fi setup, then asks for a location.
"""
import json
import math
import os
import bisect
import queue
import tempfile
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pygame

HERE = os.path.dirname(os.path.abspath(__file__))
CANVAS = 1080            # native resolution of the Waveshare round screen
C = CANVAS // 2          # centre
R = 500                  # radius of the radar area in canvas pixels
KM_PER_DEG = 111.32
EARTH_RADIUS_KM = 6378.137
SAVED_LOCATION = os.path.join(HERE, "location.json")
PREFS_FILE = os.path.join(HERE, "prefs.json")          # small local choices, e.g. pinch-to-zoom on/off
LABWC_RC = os.path.expanduser("~/.config/labwc/rc.xml")  # Raspberry Pi desktop settings (touch)
KANSHI_CFG = os.path.expanduser("~/.config/kanshi/config")  # Raspberry Pi screen settings (rotation)
SEARCH_URL = "https://photon.komoot.io/api/"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"   # ground height of your home (free, no key)
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"       # cloud cover (free, no key)
SIGHTINGS_FILE = os.path.join(HERE, "sightings.json")         # your military sightings (stays on this device)
EMERGENCY_SQUAWKS = {"7700": "GENERAL EMERGENCY", "7600": "RADIO FAILURE", "7500": "HIJACK"}

# Colours
BLACK = (0, 0, 0)
SCOPE_BG = (4, 30, 26)
RING = (15, 110, 86)
GRID = (8, 70, 56)
SWEEP = (93, 202, 165)
CIVIL = (159, 225, 203)
CIVIL_DIM = (29, 120, 95)
MIL = (240, 120, 90)
MIL_DIM = (140, 60, 40)
SELECT = (250, 199, 117)
TEXT = (159, 225, 203)
TEXT_DIM = (29, 158, 117)
WARN = (240, 149, 149)
PATH_BLUE = (70, 150, 255)       # where the selected plane has been
ROUTE_WHITE = (235, 240, 240)    # where it is going
HOME_BLUE = (66, 133, 244)       # your location dot
PANEL_BG = (2, 20, 17)           # dark boxes behind text
MAP_TINT = (110, 190, 170)       # colour the street map is tinted with
THEMES = {
    "green": dict(SCOPE_BG=(4, 30, 26), RING=(15, 110, 86), GRID=(8, 70, 56), SWEEP=(93, 202, 165),
                  CIVIL=(159, 225, 203), CIVIL_DIM=(29, 120, 95), TEXT=(159, 225, 203), TEXT_DIM=(29, 158, 117),
                  PANEL_BG=(2, 20, 17), MAP_TINT=(110, 190, 170), HOME_BLUE=(66, 133, 244),
                  MIL=(240, 120, 90), MIL_DIM=(140, 60, 40), SELECT=(250, 199, 117)),
    "amber": dict(SCOPE_BG=(28, 18, 4), RING=(130, 88, 18), GRID=(78, 52, 10), SWEEP=(245, 180, 70),
                  CIVIL=(252, 214, 150), CIVIL_DIM=(140, 98, 32), TEXT=(252, 214, 150), TEXT_DIM=(196, 142, 56),
                  PANEL_BG=(18, 12, 3), MAP_TINT=(210, 160, 80), HOME_BLUE=(90, 170, 255),
                  MIL=(255, 110, 80), MIL_DIM=(150, 55, 35), SELECT=(255, 240, 190)),
    "blue": dict(SCOPE_BG=(4, 14, 30), RING=(30, 78, 150), GRID=(14, 42, 88), SWEEP=(100, 170, 245),
                 CIVIL=(175, 212, 250), CIVIL_DIM=(50, 98, 160), TEXT=(175, 212, 250), TEXT_DIM=(95, 140, 205),
                 PANEL_BG=(3, 9, 20), MAP_TINT=(110, 150, 225), HOME_BLUE=(120, 220, 255),
                 MIL=(250, 120, 90), MIL_DIM=(140, 60, 40), SELECT=(250, 210, 120)),
    "red": dict(SCOPE_BG=(18, 0, 0), RING=(105, 12, 10), GRID=(58, 6, 5), SWEEP=(205, 40, 30),
                CIVIL=(225, 80, 64), CIVIL_DIM=(115, 24, 18), TEXT=(225, 80, 64), TEXT_DIM=(150, 36, 28),
                PANEL_BG=(12, 0, 0), MAP_TINT=(170, 45, 35), HOME_BLUE=(255, 120, 80),
                MIL=(255, 170, 80), MIL_DIM=(150, 90, 30), SELECT=(255, 220, 150)),
}
THEME_NAMES = {"green": "Green (classic)", "amber": "Amber", "blue": "Blue", "red": "Red (keeps your night vision)"}


def set_theme(name):
    globals().update(THEMES.get(name, THEMES["green"]))
ALERT_RED = (235, 50, 50)        # military aircraft close to you
ALERT_YELLOW = (250, 200, 60)    # any other aircraft very close to you

FIGHTER_SHAPE = [(0, -16), (3, -6), (4, 0), (13, 10), (13, 13), (4, 10), (3, 12), (6, 15), (6, 16),
                 (-6, 16), (-6, 15), (-3, 12), (-4, 10), (-13, 13), (-13, 10), (-4, 0), (-3, -6)]
HEAVY_SHAPE = [(0, -17), (3, -8), (19, 1), (19, 4), (3, 1), (2, 12), (7, 16), (7, 18), (0, 16),
               (-7, 18), (-7, 16), (-2, 12), (-3, 1), (-19, 4), (-19, 1), (-3, -8)]
HEAVY_ENGINES = [(-13, 1), (-7, -2), (7, -2), (13, 1)]
LIGHT_SHAPE = [(0, -10), (2, -6), (12, -5), (12, -2), (2, -2), (1, 6), (4, 8), (4, 9), (-4, 9), (-4, 8),
               (-1, 6), (-2, -2), (-12, -2), (-12, -5), (-2, -6)]
HELI_SHAPE = [(0, -8), (4, -4), (4, 4), (1, 7), (1, 14), (3, 15), (-3, 15), (-1, 14), (-1, 7), (-4, 4), (-4, -4)]
FIGHTER_TYPES = {"EUFI", "RFAL", "F35", "F22", "F16", "F15", "F15E", "F18", "F18H", "F18S", "TOR", "GRIF", "JAS39",
                 "MIR2", "A10", "HAWK", "M346", "T38", "AJET", "PC21", "F5", "MG29", "SU27", "SU30", "SU35", "J35"}
HELI_TYPES = {"H60", "NH90", "H47", "EC35", "EC45", "TIGR", "LYNX", "A139", "H64", "EC30", "EC55", "AS32", "AS65",
              "A169", "A189", "H135", "H145", "B06", "R44", "R22", "S92", "EH10", "MI8", "UH1"}
FOUR_ENGINE_TYPES = {"A400", "C17", "C5M", "K35R", "E3TF", "E3CF", "B744", "B748", "B74S", "A388", "A343", "A346",
                     "R135", "C130", "C30J", "P3", "A124", "AN22", "IL76", "B52", "E6", "C160"}
LIGHT_TYPES = {"C172", "C152", "C182", "C206", "P28A", "DA40", "DA42", "SR22", "PC12", "BE20", "C208", "DR40", "TBM9"}


def aircraft_kind(p):
    """What the icon should look like: fighter, heli, four (4-engine heavy), light or jet."""
    cat = (p.get("category") or "").upper()
    t = (p.get("t") or "").upper()
    if t in FIGHTER_TYPES or cat == "A6":
        return "fighter"
    if t in HELI_TYPES or cat == "A7":
        return "heli"
    if t in FOUR_ENGINE_TYPES:
        return "four"
    if t in LIGHT_TYPES or cat in ("A1", "B1", "B4"):
        return "light"
    return "jet"


PLANE_SHAPE = [(0, -14), (3, -5), (14, 2), (14, 5), (3, 2), (2, 10), (6, 14), (6, 16),
               (0, 14), (-6, 16), (-6, 14), (-2, 10), (-3, 2), (-14, 5), (-14, 2), (-3, -5)]


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def km_between(a, b):
    """Approximate distance in km between two (lat, lon) points (fine for short distances)."""
    dx = (b[1] - a[1]) * KM_PER_DEG * math.cos(math.radians(a[0]))
    dy = (b[0] - a[0]) * KM_PER_DEG
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- location
class Location:
    """The radar centre. Chosen with the on-screen search and saved in location.json."""

    def __init__(self, cfg):
        self.lock = threading.Lock()
        self.pos = (cfg["lat"], cfg["lon"])
        self.name = cfg.get("home_name", "Home")
        self.listeners = []
        try:
            with open(SAVED_LOCATION, encoding="utf-8") as f:
                saved = json.load(f)
            self.pos = (float(saved["lat"]), float(saved["lon"]))
            self.name = saved.get("name") or self.name
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def current(self):
        with self.lock:
            return self.pos

    def set(self, lat, lon, name):
        with self.lock:
            self.pos = (lat, lon)
            self.name = name
        try:
            with open(SAVED_LOCATION, "w", encoding="utf-8") as f:
                json.dump({"name": name, "lat": lat, "lon": lon}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        for callback in self.listeners:
            callback()


def describe_place(props):
    """Photon result -> (main line, detail line)."""
    street, number, name = props.get("street"), props.get("housenumber"), props.get("name")
    address = f"{street} {number}" if street and number else (street or "")
    main = name or address or props.get("city") or "?"
    if name and address and name != address:
        detail_first = address + ", "
    else:
        detail_first = ""
    town = " ".join(x for x in (props.get("postcode"), props.get("city") or props.get("county")) if x)
    detail = detail_first + ", ".join(x for x in (town, props.get("country")) if x)
    return main, detail


# --------------------------------------------------------------------------- multi-touch (Pi)
def load_prefs():
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(**changes):
    prefs = load_prefs()
    prefs.update(changes)
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass


def multitouch_state():
    """True = real multi-touch (pinch works), False = touch acts as a mouse, None = not a Pi desktop."""
    try:
        with open(LABWC_RC, encoding="utf-8") as f:
            xml = f.read()
    except OSError:
        return None
    if 'mouseEmulation="yes"' in xml:
        return False
    if 'mouseEmulation="no"' in xml:
        return True
    return None


def set_multitouch(on):
    """Switch the Pi desktop's touch mode and remember the choice (run.py respects it)."""
    try:
        with open(LABWC_RC, encoding="utf-8") as f:
            xml = f.read()
        backup = LABWC_RC + ".before-sky-radar"
        if not os.path.exists(backup):
            with open(backup, "w", encoding="utf-8") as f:
                f.write(xml)
        old, new = ('"yes"', '"no"') if on else ('"no"', '"yes"')
        with open(LABWC_RC, "w", encoding="utf-8") as f:
            f.write(xml.replace("mouseEmulation=" + old, "mouseEmulation=" + new))
    except OSError:
        return False
    save_prefs(multitouch=on)
    subprocess.call(["pkill", "-HUP", "-x", "labwc"])     # desktop reloads its touch settings
    return True


# --------------------------------------------------------------------------- screen rotation
def system_rotation_available():
    """True on a Wayland desktop (Raspberry Pi OS) where the real screen output can be rotated."""
    return sys.platform.startswith("linux") and bool(os.environ.get("WAYLAND_DISPLAY")) and shutil.which("wlr-randr") is not None


def _screen_output():
    """(output name, rotation in degrees) of the first screen, from wlr-randr."""
    out = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=5).stdout
    name, rot = None, 0
    for line in out.splitlines():
        if line and not line[0].isspace() and name is None:
            name = line.split()[0]
        m = re.match(r"\s*Transform:\s*(\S+)", line)
        if m and name:
            rot = int(m.group(1)) if m.group(1).isdigit() else 0
            break
    return name, rot


def get_rotation():
    if system_rotation_available():
        try:
            return _screen_output()[1]
        except Exception:
            return 0
    return int(load_prefs().get("rotation", 0)) % 360


def set_rotation(deg):
    """Rotate the screen to 0/90/180/270 degrees clockwise. On the Pi this turns the real screen output
    (touch follows) and saves it in kanshi's config so it survives reboots; elsewhere the app rotates itself."""
    deg %= 360
    if not system_rotation_available():
        save_prefs(rotation=deg)
        return
    name, _ = _screen_output()
    if not name:
        return
    value = "normal" if deg == 0 else str(deg)
    subprocess.call(["wlr-randr", "--output", name, "--transform", value])
    try:
        with open(KANSHI_CFG, encoding="utf-8") as f:
            cfg = f.read()
    except OSError:
        cfg = ""
    pattern = re.compile(r"^(\s*output\s+" + re.escape(name) + r"\b.*)$", re.M)
    line = pattern.search(cfg)
    if line:
        old = line.group(1)
        new = re.sub(r"transform\s+\S+", "transform " + value, old) if "transform" in old else old + " transform " + value
        cfg = cfg[:line.start(1)] + new + cfg[line.end(1):]
    else:
        cfg = cfg.rstrip() + ("\n" if cfg.strip() else "") + "profile {\n\toutput " + name + " enable transform " + value + "\n}\n"
    os.makedirs(os.path.dirname(KANSHI_CFG), exist_ok=True)
    with open(KANSHI_CFG, "w", encoding="utf-8") as f:
        f.write(cfg)
    subprocess.call(["pkill", "-HUP", "-x", "kanshi"])   # kanshi reloads and keeps the new rotation


# --------------------------------------------------------------------------- settings UI
def draw_button(ui, rect, label, primary=False, size=28):
    cv = ui.canvas
    pygame.draw.rect(cv, SCOPE_BG, rect, border_radius=10)
    pygame.draw.rect(cv, SWEEP if primary else RING, rect, 2 if primary else 1, border_radius=10)
    ui.blit_text(label, size, SWEEP if primary else TEXT, rect.center)


def draw_screen_bg(ui, title, subtitle=""):
    cv = ui.canvas
    cv.fill(BLACK)
    pygame.draw.circle(cv, PANEL_BG, (C, C), C)
    pygame.draw.circle(cv, RING, (C, C), C - 2, 2)
    ui.blit_text(title, 44, TEXT, (C, 150))
    if subtitle:
        ui.blit_text(subtitle, 28, TEXT_DIM, (C, 188))


def draw_text_field(ui, text, placeholder, now, y=245):
    field = pygame.Rect(0, 0, 700, 62)
    field.center = (C, y)
    pygame.draw.rect(ui.canvas, SCOPE_BG, field, border_radius=12)
    pygame.draw.rect(ui.canvas, SWEEP, field, 2, border_radius=12)
    cursor = "|" if int(now * 2) % 2 == 0 else " "
    shown = text
    while shown and ui.fonts[34].size(shown + "|")[0] > field.width - 36:
        shown = shown[1:]             # keep the end of long text visible
    if text:
        ui.blit_text(shown + cursor, 34, TEXT, (field.x + 18, field.centery), "midleft")
    else:
        ui.blit_text(placeholder + cursor, 34, TEXT_DIM, (field.x + 18, field.centery), "midleft")


class Keyboard:
    """On-screen keyboard sized for the round touch screen.

    tap() returns ("char", c), ("del",), ("action", label) or None.
    """
    LAYOUTS = {
        "abc": ["1234567890", "qwertzuiopü", "asdfghjklöä", "yxcvbnmß,.-"],
        "ABC": ["1234567890", "QWERTZUIOPÜ", "ASDFGHJKLÖÄ", "YXCVBNM;:_'"],
        "#+=": ["!@#$%^&*()", "-_=+[]{};:", "'\"\\|/<>?~`", "§°,.µ"],
    }
    KEY_W, KEY_H, GAP = 68, 62, 6
    ROW_TOP = 560

    def __init__(self, action_label):
        self.action_label = action_label
        self.mode = "abc"
        self.keys = []
        self._layout()

    def _layout(self):
        keys = []
        for i, row in enumerate(self.LAYOUTS[self.mode]):
            labels = list(row) + (["DEL"] if i == 0 else [])
            widths = [self.KEY_W] * len(row) + ([110] if i == 0 else [])
            total = sum(widths) + (len(widths) - 1) * self.GAP
            x = C - total // 2
            y = self.ROW_TOP + i * (self.KEY_H + 10)
            for label, w in zip(labels, widths):
                keys.append((pygame.Rect(x, y, w, self.KEY_H), label))
                x += w + self.GAP
        y = self.ROW_TOP + 4 * (self.KEY_H + 10)
        case_label = "abc" if self.mode == "ABC" else "ABC"
        sym_label = "abc" if self.mode == "#+=" else "#+="
        bottom = [("Back", 130), (case_label + " ", 110), (sym_label + "  ", 110), ("space", 200), (self.action_label, 170)]
        total = sum(w for _, w in bottom) + (len(bottom) - 1) * self.GAP
        x = C - total // 2
        for label, w in bottom:
            keys.append((pygame.Rect(x, y, w, self.KEY_H), label))
            x += w + self.GAP
        self.keys = keys

    def reset(self):
        self.mode = "abc"
        self._layout()

    def tap(self, x, y):
        for rect, label in self.keys:
            if not rect.collidepoint(x, y):
                continue
            if label == "DEL":
                return ("del",)
            if label == "space":
                return ("char", " ")
            if label in ("Back", self.action_label):
                return ("action", label)
            if label.endswith("  "):          # symbols toggle
                self.mode = "abc" if self.mode == "#+=" else "#+="
                self._layout()
                return None
            if label.endswith(" "):           # upper/lower case toggle
                self.mode = "abc" if self.mode == "ABC" else "ABC"
                self._layout()
                return None
            return ("char", label)
        return None

    def draw(self, ui):
        for rect, label in self.keys:
            special = len(label) > 1
            draw_button(ui, rect, label.strip(), primary=(label == self.action_label), size=28 if special else 34)


class LocationScreen:
    """Address search: type, see suggestions as you type, tap one to set the radar centre."""

    def __init__(self, settings):
        self.settings = settings
        self.location = settings.location
        self.lang = settings.cfg.get("search_lang", "default")
        self.keyboard = Keyboard("Search")
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.result_rects = []
        self.reset()
        threading.Thread(target=self._worker, daemon=True).start()

    def reset(self):
        self.text = ""
        self.searched = None
        self.last_edit = 0.0
        self.seq = getattr(self, "seq", 0) + 1
        self.keyboard.reset()
        with self.lock:
            self.results = []
            self.message = "Type an address, town or place"

    # -- input ----------------------------------------------------------------
    def type_text(self, t):
        if len(self.text) < 60:
            self.text += t
            self.last_edit = time.time()

    def key(self, k):
        if k == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            self.last_edit = time.time()
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.submit()

    def submit(self):
        with self.lock:
            results = list(self.results)
        if results and self.searched == self.text:
            self.choose(results[0])
        else:
            self._search_now()

    def choose(self, result):
        lat, lon, main, _ = result
        self.location.set(lat, lon, main)
        self.settings.done()

    def tap(self, x, y):
        for rect, result in self.result_rects:
            if rect.collidepoint(x, y):
                self.choose(result)
                return
        ev = self.keyboard.tap(x, y)
        if not ev:
            return
        if ev[0] == "char":
            self.type_text(ev[1])
        elif ev[0] == "del":
            self.key(pygame.K_BACKSPACE)
        elif ev[1] == "Back":
            self.settings.done()
        else:
            self.submit()

    # -- searching --------------------------------------------------------------
    def _search_now(self):
        q = self.text.strip()
        if len(q) < 3:
            with self.lock:
                self.message = "Type at least 3 letters"
            return
        self.searched = self.text
        self.seq += 1
        with self.lock:
            self.message = "Searching..."
        self.jobs.put((self.seq, q))

    def update(self, now):
        # search as you type: start once typing pauses for 0.4 s
        if self.text != self.searched and now - self.last_edit > 0.4 and len(self.text.strip()) >= 3:
            self._search_now()

    def _worker(self):
        while True:
            seq, q = self.jobs.get()
            if seq != self.seq:
                continue                          # a newer search is already waiting
            lat, lon = self.location.current()    # prefer results near the current centre
            params = urllib.parse.urlencode({"q": q, "limit": 4, "lang": self.lang,
                                             "lat": round(lat, 1), "lon": round(lon, 1)})
            try:
                req = urllib.request.Request(f"{SEARCH_URL}?{params}", headers={"User-Agent": "sky-radar/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    features = json.load(resp).get("features", [])
                results = []
                for feat in features:
                    flon, flat = feat["geometry"]["coordinates"][:2]
                    main, detail = describe_place(feat.get("properties", {}))
                    results.append((flat, flon, main, detail))
                message = "" if results else "No matches. Try adding the town or postcode."
            except Exception as e:
                results, message = [], f"Search failed ({type(e).__name__}). Check the Wi-Fi."
            if seq == self.seq:
                with self.lock:
                    self.results, self.message = results, message

    # -- drawing ------------------------------------------------------------------
    def draw(self, ui, now):
        draw_screen_bg(ui, "Set location", f"Now: {self.location.name}")
        draw_text_field(ui, self.text, "Street and number, town", now)
        with self.lock:
            results, message = list(self.results), self.message
        self.result_rects = []
        y = 292
        for res in results:
            rect = pygame.Rect(0, 0, 760, 60)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=10)
            pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=10)
            ui.blit_text(res[2][:40], 28, SELECT, (rect.x + 18, rect.y + 8), "topleft")
            ui.blit_text(res[3][:62], 24, TEXT_DIM, (rect.x + 18, rect.y + 35), "topleft")
            self.result_rects.append((rect, res))
            y += 65
        if message:
            ui.blit_text(message, 28, TEXT_DIM, (C, 320))
        self.keyboard.draw(ui)


# --------------------------------------------------------------------------- Wi-Fi
class NmcliWifi:
    """Wi-Fi through NetworkManager (Raspberry Pi OS Bookworm and newer).

    NetworkManager stores the password in its own protected config and
    reconnects automatically at boot, so this app never saves passwords itself.
    """

    def __init__(self):
        self.available = sys.platform.startswith("linux") and shutil.which("nmcli") is not None
        self.unavailable_reason = ("Wi-Fi setup needs NetworkManager (Raspberry Pi OS). "
                                   "On this computer, use the system Wi-Fi settings.")

    @staticmethod
    def _run(args, timeout=20):
        return subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _fields(line):
        """Split nmcli terse output, which escapes ':' and '\\' with a backslash."""
        parts, cur, esc = [], "", False
        for ch in line:
            if esc:
                cur, esc = cur + ch, False
            elif ch == "\\":
                esc = True
            elif ch == ":":
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        return parts

    def scan(self):
        out = self._run(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"], 40)
        if out.returncode:
            raise RuntimeError(out.stderr.strip() or "Scan failed")
        nets = {}
        for line in out.stdout.splitlines():
            f = self._fields(line)
            if len(f) < 4 or not f[1]:
                continue                      # hidden networks have no name
            active = f[0].strip() == "*"
            signal = int(f[2]) if f[2].isdigit() else 0
            old = nets.get(f[1])
            if old is None or signal > old["signal"] or active:
                nets[f[1]] = {"ssid": f[1], "signal": signal, "secure": f[3] not in ("", "--"),
                              "active": active or bool(old and old["active"])}
        return sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"]))

    def current(self):
        out = self._run(["-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"], 10)
        for line in out.stdout.splitlines():
            f = self._fields(line)
            if len(f) >= 2 and f[0] == "yes":
                return f[1]
        return None

    def connect(self, ssid, password):
        known = [self._fields(l)[0] for l in self._run(["-t", "-f", "NAME", "connection", "show"]).stdout.splitlines()]
        args = ["device", "wifi", "connect", ssid] + (["password", password] if password else [])
        out = self._run(args, 60)
        if out.returncode == 0:
            return True, f"Connected to {ssid}"
        if ssid not in known:                 # don't leave a broken profile behind
            self._run(["connection", "delete", "id", ssid])
        err = (out.stderr or out.stdout).strip()
        low = err.lower()
        if "secrets were required" in low or "psk" in low or "password" in low:
            return False, "Wrong password. Check it and try again."
        if "not authorized" in low or "insufficient privileges" in low:
            return False, "Not allowed to change Wi-Fi from this user (see README)."
        if "no network with ssid" in low:
            return False, "Network not found. Move closer and rescan."
        return False, (err.splitlines()[-1][:70] if err else "Connection failed.")


class FakeWifi:
    """Pretend Wi-Fi for trying the screens on a PC: set SKY_RADAR_FAKE_WIFI=1. Password is 'password123'."""
    available = True
    unavailable_reason = ""

    def __init__(self):
        self.connected = None

    def scan(self):
        time.sleep(1)
        nets = [("HomeNet", 82, True), ("HomeNet-5G", 64, True), ("Neighbour", 41, True), ("Cafe Free WiFi", 30, False)]
        return [{"ssid": s, "signal": g, "secure": sec, "active": s == self.connected} for s, g, sec in nets]

    def current(self):
        return self.connected

    def connect(self, ssid, password):
        time.sleep(2)
        if password in ("", "password123"):
            self.connected = ssid
            return True, f"Connected to {ssid}"
        return False, "Wrong password. Check it and try again."


HEARTBEAT_FILE = os.path.join(tempfile.gettempdir(), "sky-radar-heartbeat")
WATCHDOG_FILE = os.path.join(HERE, "watchdog.json")


def watchdog_label():
    if not os.environ.get("SKY_RADAR_WATCHDOG"):
        return "Off - start Sky Radar with run.py to turn it on"
    try:
        with open(WATCHDOG_FILE, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return "On - no restarts needed"
    when = time.strftime("%d %b ", time.localtime(info.get("last", 0))) + fmt_clock(info.get("last", 0))
    return f"On - restarted {info.get('restarts', 0)}x, last {when} ({info.get('reason', '?')})"


def internet_ok(timeout=3):
    try:
        socket.create_connection(("photon.komoot.io", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


class WifiScreen:
    PAGE = 6

    def __init__(self, settings):
        self.settings = settings
        self.wifi = settings.wifi
        self.keyboard = Keyboard("Connect")
        self.lock = threading.Lock()
        self.rects = []
        self.state = "list"
        self.networks = []
        self.page = 0
        self.message = ""
        self.target = None
        self.password = ""
        self.show_pw = False
        self.result_ok = False
        self.result_time = 0.0

    def reset(self):
        self.state = "list"
        self.page = 0
        self.password = ""
        self.show_pw = False
        self.rescan()

    def rescan(self):
        if not self.wifi.available:
            self.message = self.wifi.unavailable_reason
            return
        with self.lock:
            self.message = "Scanning for networks..."
            self.networks = []

        def work():
            try:
                nets = self.wifi.scan()
                msg = "" if nets else "No networks found. Tap Rescan."
            except Exception as e:
                nets, msg = [], f"Scan failed: {str(e)[:60]}"
            with self.lock:
                self.networks, self.message, self.page = nets, msg, 0
        threading.Thread(target=work, daemon=True).start()

    def pick(self, net):
        self.target = net
        self.password = ""
        self.show_pw = False
        self.keyboard.reset()
        if net["secure"]:
            self.state = "password"
        else:
            self.start_connect()

    def start_connect(self):
        self.state = "connecting"
        ssid, pw = self.target["ssid"], self.password

        def work():
            try:
                ok, msg = self.wifi.connect(ssid, pw)
            except Exception as e:
                ok, msg = False, f"Connection failed: {type(e).__name__}"
            with self.lock:
                self.result_ok, self.message, self.result_time = ok, msg, time.time()
                self.state = "result"
        threading.Thread(target=work, daemon=True).start()

    # -- input ----------------------------------------------------------------
    def type_text(self, t):
        if self.state == "password" and len(self.password) < 63:
            self.password += t

    def key(self, k):
        if self.state != "password":
            return
        if k == pygame.K_BACKSPACE:
            self.password = self.password[:-1]
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER) and len(self.password) >= 8:
            self.start_connect()

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                self.rects = []           # buttons belong to the screen that was drawn; it may change now
                action()
                return
        if self.state == "password":
            ev = self.keyboard.tap(x, y)
            if not ev:
                return
            if ev[0] == "char":
                self.type_text(ev[1])
            elif ev[0] == "del":
                self.password = self.password[:-1]
            elif ev[1] == "Back":
                self.state = "list"
            elif len(self.password) >= 8:
                self.start_connect()

    def update(self, now):
        if self.state == "result" and self.result_ok and now - self.result_time > 1.5:
            self.settings.wifi_changed()
            self.settings.done()

    # -- drawing ------------------------------------------------------------------
    def _add_button(self, ui, rect, label, action, primary=False):
        draw_button(ui, rect, label, primary)
        self.rects.append((rect, action))

    def draw(self, ui, now):
        self.rects = []
        with self.lock:
            state, nets, message = self.state, list(self.networks), self.message
        if state == "list":
            self._draw_list(ui, nets, message)
        elif state == "password":
            self._draw_password(ui, now)
        elif state == "connecting":
            draw_screen_bg(ui, "Wi-Fi")
            dots = "." * (int(now * 2) % 4)
            ui.blit_text(f"Connecting to {self.target['ssid']}{dots}", 34, TEXT, (C, 450))
            ui.blit_text("This can take up to 30 seconds", 28, TEXT_DIM, (C, 500))
        else:
            draw_screen_bg(ui, "Wi-Fi")
            ui.blit_text(message, 34, SWEEP if self.result_ok else WARN, (C, 450))
            if not self.result_ok:
                self._add_button(ui, pygame.Rect(C - 310, 560, 300, 70), "Try again", lambda: self.pick(self.target), True)
                self._add_button(ui, pygame.Rect(C + 10, 560, 300, 70), "Networks", self.reset)

    def _draw_list(self, ui, nets, message):
        current = next((n["ssid"] for n in nets if n["active"]), None)
        draw_screen_bg(ui, "Wi-Fi", f"Connected to {current}" if current else "Choose a network")
        y = 230
        for net in nets[self.page * self.PAGE:(self.page + 1) * self.PAGE]:
            rect = pygame.Rect(0, 0, 760, 60)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=10)
            pygame.draw.rect(ui.canvas, SWEEP if net["active"] else RING, rect, 2 if net["active"] else 1, border_radius=10)
            ui.blit_text(net["ssid"][:32], 34, SELECT if net["active"] else TEXT, (rect.x + 20, rect.centery), "midleft")
            self._draw_signal(ui, rect.right - 60, rect.centery + 14, net["signal"])
            if net["secure"]:
                self._draw_lock(ui, rect.right - 100, rect.centery)
            self.rects.append((rect, lambda n=net: self.pick(n)))
            y += 66
        if message:
            ui.blit_text(message, 28, TEXT_DIM, (C, 300) if not nets else (C, y + 20))
        buttons = [("Back", self.settings.done), ("Rescan", self.rescan)]
        if len(nets) > self.PAGE:
            buttons.append(("More", self._next_page))
        width = 200
        total = len(buttons) * width + (len(buttons) - 1) * 12
        x = C - total // 2
        for label, action in buttons:
            self._add_button(ui, pygame.Rect(x, 740, width, 70), label, action)
            x += width + 12

    def _next_page(self):
        pages = (len(self.networks) + self.PAGE - 1) // self.PAGE
        self.page = (self.page + 1) % max(1, pages)

    def _draw_password(self, ui, now):
        draw_screen_bg(ui, "Wi-Fi password", self.target["ssid"][:36])
        shown = self.password if self.show_pw else "*" * len(self.password)
        draw_text_field(ui, shown, "Password", now)
        self._add_button(ui, pygame.Rect(C - 150, 300, 300, 56),
                         "Hide password" if self.show_pw else "Show password", self._toggle_show)
        hint = "At least 8 characters" if len(self.password) < 8 else "Tap Connect"
        ui.blit_text(hint, 28, TEXT_DIM, (C, 400))
        ui.blit_text("Saved by the system and reconnected automatically", 24, TEXT_DIM, (C, 440))
        self.keyboard.draw(ui)

    def _toggle_show(self):
        self.show_pw = not self.show_pw

    @staticmethod
    def _draw_signal(ui, x, bottom, signal):
        bars = 1 + min(3, signal // 25)
        for i in range(4):
            h = 8 + i * 7
            col = SWEEP if i < bars else GRID
            pygame.draw.rect(ui.canvas, col, pygame.Rect(x + i * 10, bottom - h, 7, h))

    @staticmethod
    def _draw_lock(ui, x, cy):
        pygame.draw.rect(ui.canvas, TEXT_DIM, pygame.Rect(x - 9, cy - 2, 18, 14), border_radius=3)
        pygame.draw.arc(ui.canvas, TEXT_DIM, pygame.Rect(x - 7, cy - 13, 14, 20), 0, math.pi, 3)


# --------------------------------------------------------------------------- alert directions
DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
DIRECTION_NAMES = {"N": "NORTH", "NE": "NORTH-EAST", "E": "EAST", "SE": "SOUTH-EAST",
                   "S": "SOUTH", "SW": "SOUTH-WEST", "W": "WEST", "NW": "NORTH-WEST"}


def compass_direction(east_km, north_km):
    """Which of the 8 compass directions a point lies in, seen from you (each covers 45 degrees)."""
    bearing = math.degrees(math.atan2(east_km, north_km)) % 360
    return DIRECTIONS[int(((bearing + 22.5) % 360) // 45)]


def describe_directions(dirs):
    if len(dirs) == len(DIRECTIONS):
        return "All directions"
    if not dirs:
        return "None - alerts are off"
    return ", ".join(d for d in DIRECTIONS if d in dirs)


class DirectionsScreen:
    """Compass with 8 sections: tap the directions you can see from (e.g. a west-facing balcony)."""
    CX, CY, R_OUT, R_IN = C, 520, 290, 85

    def __init__(self, settings):
        self.settings = settings
        self.rects = []

    def reset(self):
        pass

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def _toggle(self, d):
        dirs = set(self.settings.alert_dirs)
        dirs.symmetric_difference_update({d})
        self.settings.set_alert_dirs(dirs)

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return
        dx, dy = x - self.CX, y - self.CY
        if self.R_IN <= math.hypot(dx, dy) <= self.R_OUT:
            self._toggle(compass_direction(dx, -dy))     # screen y grows downwards

    def _pt(self, angle_deg, r):
        a = math.radians(angle_deg)
        return (self.CX + r * math.sin(a), self.CY - r * math.cos(a))

    def draw(self, ui, now):
        self.rects = []
        dirs = self.settings.alert_dirs
        draw_screen_bg(ui, "Alert directions", "Tap the directions you can see from")
        for i, d in enumerate(DIRECTIONS):
            c = i * 45
            outer = [self._pt(c - 22.5 + k * 4.5, self.R_OUT) for k in range(11)]
            inner = [self._pt(c + 22.5 - k * 4.5, self.R_IN) for k in range(11)]
            on = d in dirs
            pygame.draw.polygon(ui.canvas, (20, 95, 75) if on else SCOPE_BG, outer + inner)
            pygame.draw.polygon(ui.canvas, SWEEP if on else RING, outer + inner, 2)
            ui.blit_text(d, 44 if len(d) == 1 else 34, (240, 245, 245) if on else TEXT_DIM, self._pt(c, (self.R_IN + self.R_OUT) / 2))
        pygame.draw.circle(ui.canvas, HOME_BLUE, (self.CX, self.CY), 12)       # you, in the middle
        pygame.draw.circle(ui.canvas, (255, 255, 255), (self.CX, self.CY), 12, 3)
        ui.blit_text(describe_directions(dirs), 28, TEXT, (C, self.CY + self.R_OUT + 32))
        for label, x, action in (("All", C - 230, lambda: self.settings.set_alert_dirs(set(DIRECTIONS))),
                                 ("Done", C + 30, self.settings.done)):
            rect = pygame.Rect(x, 870, 200, 66)
            draw_button(ui, rect, label, primary=(label == "Done"))
            self.rects.append((rect, action))


class SightingsScreen:
    """List of military aircraft seen near home: today, this week and the last 90 days."""
    PAGE = 6

    def __init__(self, settings):
        self.settings = settings
        self.rects = []
        self.page = 0
        self.mode = "list"

    def reset(self):
        self.page = 0
        self.mode = "list"

    def _set_mode(self, mode):
        self.mode = mode

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return

    def _more(self, total):
        pages = max(1, (total + self.PAGE - 1) // self.PAGE)
        self.page = (self.page + 1) % pages

    def _buttons(self, ui, buttons, y=745):
        width = 200 if len(buttons) < 4 else 170
        x = C - (len(buttons) * width + (len(buttons) - 1) * 12) // 2
        for label, action in buttons:
            rect = pygame.Rect(x, y, width, 66)
            draw_button(ui, rect, label, primary=(label.startswith("Back")))
            self.rects.append((rect, action))
            x += width + 12

    def _draw_times(self, ui, log):
        grid = log.best_times()
        draw_screen_bg(ui, "Best time to watch", "When military jets passed near you (last 90 days)")
        total = sum(map(sum, grid))
        if not total:
            ui.blit_text("Not enough sightings yet.", 28, TEXT_DIM, (C, 450))
        else:
            day, slot = max(((d, h) for d in range(7) for h in range(12)), key=lambda dh: grid[dh[0]][dh[1]])
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            busiest_day = max(range(7), key=lambda d: sum(grid[d]))
            busiest_slot = max(range(12), key=lambda h: sum(grid[d][h] for d in range(7)))
            ui.blit_text(f"Most often: {days[day]}, {slot * 2:02d}:00-{slot * 2 + 2:02d}:00", 34, SELECT, (C, 230))
            ui.blit_text(f"Busiest day {days[busiest_day]}  |  busiest time {busiest_slot * 2:02d}:00-{busiest_slot * 2 + 2:02d}:00",
                         24, TEXT_DIM, (C, 270))
            peak = max(max(row) for row in grid)
            cw, ch, x0, y0 = 50, 44, C - 300 + 30, 310
            for d in range(7):
                ui.blit_text(days[d], 24, TEXT_DIM, (x0 - 12, y0 + d * ch + ch // 2), "midright")
                for h in range(12):
                    v = grid[d][h] / peak if peak else 0
                    col = tuple(int(SCOPE_BG[i] + (MIL[i] - SCOPE_BG[i]) * v) for i in range(3))
                    cell = pygame.Rect(x0 + h * cw, y0 + d * ch, cw - 4, ch - 4)
                    pygame.draw.rect(ui.canvas, col, cell, border_radius=6)
                    if grid[d][h]:
                        ui.blit_text(str(grid[d][h]), 22, (240, 245, 245), cell.center)
            for h in range(0, 12, 2):
                ui.blit_text(f"{h * 2:02d}", 22, TEXT_DIM, (x0 + h * cw + (cw - 4) // 2, y0 + 7 * ch + 14))
        self._buttons(ui, [("Back to list", lambda: self._set_mode("list"))], y=680)

    def _draw_records(self, ui, log):
        draw_screen_bg(ui, "Records", "Military sightings near you (last 90 days)")
        recs = log.records()
        if not recs:
            ui.blit_text("No sightings yet.", 28, TEXT_DIM, (C, 450))
        y = 250
        for label, text in recs:
            rect = pygame.Rect(0, 0, 760, 74)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=12)
            pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=12)
            ui.blit_text(label, 24, TEXT_DIM, (rect.x + 20, rect.y + 10), "topleft")
            ui.blit_text(text[:56], 28, MIL, (rect.x + 20, rect.y + 38), "topleft")
            y += 82
        self._buttons(ui, [("Back to list", lambda: self._set_mode("list"))], y=y + 20)

    def draw(self, ui, now):
        self.rects = []
        log = ui.sightings
        if self.mode == "times":
            return self._draw_times(ui, log)
        if self.mode == "records":
            return self._draw_records(ui, log)
        entries = sorted(log.entries, key=lambda e: e.get("last", 0), reverse=True)
        draw_screen_bg(ui, "Military sightings",
                       f"Today {log.today()}  |  This week {log.count_since(7 * 86400)}  |  90 days {len(entries)}")
        if not entries:
            ui.blit_text(f"No military aircraft within {fmt_dist(log.radius)} yet.", 28, TEXT_DIM, (C, 430))
            ui.blit_text("They are logged here automatically.", 28, TEXT_DIM, (C, 470))
        y = 235
        for e in entries[self.page * self.PAGE:(self.page + 1) * self.PAGE]:
            rect = pygame.Rect(0, 0, 760, 74)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=12)
            pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=12)
            when = time.strftime("%a %d %b  ", time.localtime(e.get("first", 0))) + fmt_clock(e.get("first", 0))
            ui.blit_text(f"{e.get('callsign', '?')}  -  {e.get('model', '?')}"[:40], 28, MIL, (rect.x + 20, rect.y + 10), "topleft")
            alt = fmt_alt_ft(e["min_alt_ft"]) if isinstance(e.get("min_alt_ft"), int) else e.get("alt", "")
            ui.blit_text(f"{when}   closest {fmt_dist(e.get('closest_km', 0))} {e.get('dir', '')}   {alt}"[:62],
                         24, TEXT_DIM, (rect.x + 20, rect.y + 44), "topleft")
            y += 82
        buttons = [("Back", self.settings.done), ("Best times", lambda: self._set_mode("times")),
                   ("Records", lambda: self._set_mode("records"))]
        if len(entries) > self.PAGE:
            buttons.append(("More", lambda: self._more(len(entries))))
        self._buttons(ui, buttons)


AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORTS_FILE = os.path.join(HERE, "airports.json")       # downloaded once, git-ignored


class Airports:
    """Airports with scheduled flights (OurAirports, public domain), downloaded once and kept in airports.json."""

    def __init__(self):
        self.list, self.state = None, "idle"

    def ensure(self):
        if self.list is not None or self.state == "loading":
            return
        try:
            with open(AIRPORTS_FILE, encoding="utf-8") as f:
                self.list = json.load(f)
            return
        except (OSError, ValueError):
            pass
        self.state = "loading"
        threading.Thread(target=self._download, daemon=True).start()

    def _download(self):
        import csv
        try:
            req = urllib.request.Request(AIRPORTS_URL, headers={"User-Agent": "sky-radar/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                rows = csv.DictReader(io_text(resp.read()))
            keep = [[r["iata_code"], r["name"], r["municipality"], float(r["latitude_deg"]), float(r["longitude_deg"])]
                    for r in rows if r["type"] in ("large_airport", "medium_airport")
                    and r["scheduled_service"] == "yes" and r["iata_code"]]
            with open(AIRPORTS_FILE, "w", encoding="utf-8") as f:
                json.dump(keep, f)
            self.list, self.state = keep, "ready"
        except Exception as e:
            self.state = f"failed ({type(e).__name__})"

    def nearest(self, lat, lon, n=3):
        if not self.list:
            return []
        return sorted(self.list, key=lambda a: great_circle_km(lat, lon, a[3], a[4]))[:n]


def io_text(raw):
    import io
    return io.StringIO(raw.decode("utf-8", "replace"))


class AirportScreen:
    """Live arrivals and departures at the nearest airports, worked out from the planes around it."""

    def __init__(self, settings):
        self.settings = settings
        self.rects = []
        self.index = 0

    def reset(self):
        self.index = 0
        self.settings.ui.airports.ensure()

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return

    def _next(self):
        self.index = (self.index + 1) % 3

    @staticmethod
    def board(airport, aircraft):
        """(arrivals, departures): lists of (minutes, plane), arrivals soonest first, departures latest first."""
        _, _, _, alat, alon = airport
        arrivals, departures = [], []
        for p in aircraft.values():
            if "lat" not in p:
                continue
            d = great_circle_km(p["lat"], p["lon"], alat, alon)
            alt, gs = p.get("alt_baro"), p.get("gs") or 0
            rate = p.get("baro_rate") if isinstance(p.get("baro_rate"), (int, float)) else p.get("geom_rate") or 0
            if alt == "ground" or not isinstance(alt, (int, float)) or alt > 18000 or gs < 60 or d > 90:
                continue
            to_airport = math.degrees(math.atan2(math.radians(alon - p["lon"]) * math.cos(math.radians(alat)),
                                                 math.radians(alat - p["lat"]))) % 360
            off = abs((p.get("track") or 0) - to_airport) % 360
            off = min(off, 360 - off)
            minutes = d / (gs * 1.852 / 60)
            if off < 60 and (rate <= -200 or alt < 6000):
                arrivals.append((minutes * 1.15, p))
            elif off > 100 and rate >= 300 and d < 60:
                departures.append((minutes, p))
        return sorted(arrivals, key=lambda m: m[0])[:3], sorted(departures, key=lambda m: m[0])[:3]

    def _row(self, ui, y, minutes, p, arriving):
        info = ui.info.get(p)
        rect = pygame.Rect(0, 0, 760, 58)
        rect.midtop = (C, y)
        pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=12)
        pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=12)
        who = info.get("operator") or ""
        ui.blit_text(f"{callsign(p)}  {who}"[:34], 28, SELECT, (rect.x + 18, rect.y + 7), "topleft")
        when = f"in {max(1, round(minutes))} min" if arriving else f"{max(1, round(minutes))} min ago"
        ui.blit_text(when, 28, TEXT, (rect.right - 18, rect.y + 7), "topright")
        place = info.get("origin") if arriving else info.get("dest")
        where = f"{'from' if arriving else 'to'} {place['code']} {place['city']}" if place else "route not known"
        ui.blit_text(f"{where}  |  {info.get('model') or p.get('t') or ''}  |  {fmt_alt(p)}"[:66], 22, TEXT_DIM,
                     (rect.x + 18, rect.y + 35), "topleft")

    def draw(self, ui, now):
        self.rects = []
        airports = ui.airports
        lat, lon = ui.location.current()
        near = airports.nearest(lat, lon)
        if not near:
            draw_screen_bg(ui, "Airport board")
            msg = "Downloading the airport list (once)..." if airports.state in ("loading", "idle") else \
                f"Couldn't load the airport list: {airports.state}"
            ui.blit_text(msg, 28, TEXT_DIM, (C, 450))
        else:
            ap = near[self.index % len(near)]
            dist = great_circle_km(lat, lon, ap[3], ap[4])
            draw_screen_bg(ui, f"{ap[0]}  {ap[1]}"[:34], f"{fmt_dist(round(dist))} away  |  live, from planes around it")
            aircraft = ui.feed.snapshot()[0] if ui.feed else {}
            arrivals, departures = self.board(ap, aircraft)
            ui.blit_text("Arriving", 28, TEXT, (C - 380, 222), "midleft")
            y = 242
            for minutes, p in arrivals:
                self._row(ui, y, minutes, p, True)
                y += 64
            if not arrivals:
                ui.blit_text("No arrivals in the next minutes", 24, TEXT_DIM, (C, 280))
                y = 320
            ui.blit_text("Departed", 28, TEXT, (C - 380, y + 22), "midleft")
            y += 42
            for minutes, p in departures:
                self._row(ui, y, minutes, p, False)
                y += 64
            if not departures:
                ui.blit_text("No recent departures", 24, TEXT_DIM, (C, y + 38))
        x = C - (2 * 220 + 16) // 2
        for label, action, primary in (("Back", self.settings.done, True), ("Next airport", self._next, False)):
            rect = pygame.Rect(x, 760, 220, 64)
            draw_button(ui, rect, label, primary=primary)
            self.rects.append((rect, action))
            x += 236


class FeedStatusScreen:
    """Which flight-data services Sky Radar uses and how they are doing."""

    def __init__(self, settings):
        self.settings = settings
        self.rects = []

    def reset(self):
        pass

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return

    def draw(self, ui, now):
        self.rects = []
        feed = ui.feed
        draw_screen_bg(ui, "Feed status", f"{feed.count if feed else 0} aircraft in the last update  |  every {ui.cfg['poll_seconds']} s")
        y = 240
        for name, _ in (feed._sources() if feed else []):
            st = feed.stats.get(name, {})
            rest = feed.resting.get(name, 0) - now
            if name == feed.source and now - st.get("ok", 0) < 3 * ui.cfg["poll_seconds"]:
                state, col = f"In use - last update {int(now - st['ok'])} s ago", SWEEP
            elif rest > 0:
                state, col = f"Rate-limited - resting for {fmt_duration(rest / 3600)}", WARN
            elif st.get("error"):
                state, col = f"Problem: {st['error']} ({fmt_duration((now - st['error_at']) / 3600)} ago)", WARN
            elif st.get("ok"):
                state, col = "OK - standby", TEXT_DIM
            else:
                state, col = "Backup - not needed yet", TEXT_DIM
            rect = pygame.Rect(0, 0, 760, 92)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=12)
            pygame.draw.rect(ui.canvas, col if col != TEXT_DIM else RING, rect, 2 if col != TEXT_DIM else 1, border_radius=12)
            ui.blit_text(name, 34, TEXT, (rect.x + 22, rect.y + 12), "topleft")
            ui.blit_text(f"{st.get('today', 0)} requests today", 24, TEXT_DIM, (rect.right - 22, rect.y + 16), "topright")
            ui.blit_text(state, 28, col, (rect.x + 22, rect.y + 54), "topleft")
            y += 104
        ui.blit_text("The first service that answers is used. If one is busy or rate-limited,", 24, TEXT_DIM, (C, y + 20))
        ui.blit_text("Sky Radar switches to the next one by itself.", 24, TEXT_DIM, (C, y + 48))
        rect = pygame.Rect(0, 0, 220, 64)
        rect.midtop = (C, 760)
        draw_button(ui, rect, "Back", primary=True)
        self.rects.append((rect, self.settings.done))


class PlacesScreen:
    """Saved places: switch the radar centre with one tap (up to 6)."""
    MAX = 6

    def __init__(self, settings):
        self.settings = settings
        self.rects = []
        self.removing = False

    def reset(self):
        self.removing = False

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return

    @staticmethod
    def places():
        saved = load_prefs().get("places", [])
        return saved if isinstance(saved, list) else []

    def _save_current(self):
        loc = self.settings.location
        lat, lon = loc.current()
        places = [pl for pl in self.places() if pl.get("name") != loc.name][:self.MAX - 1]
        places.insert(0, {"name": loc.name, "lat": lat, "lon": lon})
        save_prefs(places=places)

    def _use(self, pl):
        if self.removing:
            save_prefs(places=[x for x in self.places() if x != pl])
            return
        self.settings.location.set(pl["lat"], pl["lon"], pl["name"])
        self.settings.done()

    def _toggle_remove(self):
        self.removing = not self.removing

    def draw(self, ui, now):
        self.rects = []
        places = self.places()
        draw_screen_bg(ui, "Saved places", "Tap one to remove it" if self.removing else "Tap a place to move the radar there")
        cur = self.settings.location.name
        y = 230
        for pl in places:
            rect = pygame.Rect(0, 0, 720, 70)
            rect.midtop = (C, y)
            on = pl.get("name") == cur
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=12)
            pygame.draw.rect(ui.canvas, WARN if self.removing else (SWEEP if on else RING), rect, 2 if on or self.removing else 1,
                             border_radius=12)
            ui.blit_text(pl.get("name", "?")[:36], 34, SELECT if on else TEXT, (rect.x + 24, rect.centery), "midleft")
            ui.blit_text("remove" if self.removing else ("current" if on else ">"), 28 if self.removing or on else 44,
                         WARN if self.removing else SWEEP, (rect.right - 30, rect.centery), "midright")
            self.rects.append((rect, lambda pl=pl: self._use(pl)))
            y += 80
        if not places:
            ui.blit_text("No saved places yet.", 28, TEXT_DIM, (C, 420))
            ui.blit_text("Save the current one, or pick a new location first.", 24, TEXT_DIM, (C, 456))
        buttons = [("Back", self.settings.done, True), ("Save current", self._save_current, False)]
        if places:
            buttons.append(("Done removing" if self.removing else "Remove", self._toggle_remove, False))
        x = C - (len(buttons) * 210 + (len(buttons) - 1) * 12) // 2
        for label, action, primary in buttons:
            rect = pygame.Rect(x, 760, 210, 64)
            draw_button(ui, rect, label, primary=primary)
            self.rects.append((rect, action))
            x += 222


class TrafficScreen:
    """Planes and military jets within 50 km of you over the last 24 hours."""

    def __init__(self, settings):
        self.settings = settings
        self.rects = []

    def reset(self):
        pass

    def update(self, now):
        pass

    def type_text(self, t):
        pass

    def key(self, k):
        pass

    def tap(self, x, y):
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                action()
                return

    def draw(self, ui, now):
        self.rects = []
        samples = [smp for smp in ui.traffic if now - smp[0] <= 86400]
        draw_screen_bg(ui, "Traffic", "Aircraft within 50 km of you, last 24 hours")
        x0, y0, w, h = C - 330, 300, 660, 330
        pygame.draw.rect(ui.canvas, SCOPE_BG, pygame.Rect(x0 - 10, y0 - 10, w + 20, h + 50), border_radius=12)
        if not samples:
            ui.blit_text("Collecting data - check back in a little while.", 28, TEXT_DIM, (C, y0 + h // 2))
        else:
            buckets = [[0, 0] for _ in range(48)]               # 30-minute bars, newest on the right
            for ts, total, mil in samples:
                b = 47 - int((now - ts) // 1800)
                if 0 <= b < 48:
                    buckets[b][0] = max(buckets[b][0], total)
                    buckets[b][1] = max(buckets[b][1], mil)
            peak = max(1, max(t for t, _ in buckets))
            bw = w / 48
            for i, (total, mil) in enumerate(buckets):
                th = int(h * total / peak)
                mh = int(h * mil / peak) if mil else 0
                pygame.draw.rect(ui.canvas, CIVIL_DIM, pygame.Rect(x0 + i * bw, y0 + h - th, bw - 2, th))
                if mil:
                    pygame.draw.rect(ui.canvas, MIL, pygame.Rect(x0 + i * bw, y0 + h - max(mh, 4), bw - 2, max(mh, 4)))
            for hours_ago in (24, 18, 12, 6, 0):
                x = x0 + w - hours_ago * 2 * bw
                ui.blit_text(fmt_clock(now - hours_ago * 3600), 22, TEXT_DIM, (x, y0 + h + 22))
            busiest = max(range(48), key=lambda i: buckets[i][0])
            ui.blit_text(f"Peak: {buckets[busiest][0]} aircraft around {fmt_clock(now - (47 - busiest) * 1800)}"
                         f"   |   military seen: {sum(1 for b in buckets if b[1])} half-hours", 24, TEXT, (C, 255))
            ui.blit_text("teal = all aircraft   coral = military", 22, TEXT_DIM, (C, y0 + h + 58))
        rect = pygame.Rect(0, 0, 220, 64)
        rect.midtop = (C, 760)
        draw_button(ui, rect, "Back", primary=True)
        self.rects.append((rect, self.settings.done))


# --------------------------------------------------------------------------- settings menu
class Settings:
    """Settings menu (gear button) and the first-run setup: Wi-Fi, then location."""

    def __init__(self, ui, location, cfg):
        self.ui = ui
        self.location = location
        self.cfg = cfg
        self.wifi = FakeWifi() if os.environ.get("SKY_RADAR_FAKE_WIFI") else NmcliWifi()
        self.screens = {"location": LocationScreen(self), "wifi": WifiScreen(self), "directions": DirectionsScreen(self),
                        "sightings": SightingsScreen(self), "airport": AirportScreen(self),
                        "feed": FeedStatusScreen(self), "places": PlacesScreen(self), "traffic": TrafficScreen(self)}
        prefs = load_prefs()
        self.max_alt = int(prefs.get("max_alt_ft", 0) or 0)            # 0 = show all heights
        self.theme = prefs.get("theme", "green") if prefs.get("theme") in THEMES else "green"
        self.text_large = load_prefs().get("text_size") == "large"
        self.night_mode = load_prefs().get("night_mode", "off")      # "off", "dim" or "blank"
        self.menu_page = 0
        u = load_prefs().get("units")
        if isinstance(u, list) and tuple(u) in UNIT_PRESETS:
            set_units(*u)
        saved = load_prefs().get("alert_directions", DIRECTIONS)
        self.alert_dirs = {d for d in saved if d in DIRECTIONS} if isinstance(saved, list) else set(DIRECTIONS)
        self.screen = None            # None = radar, "menu", "location" or "wifi"
        self.flow = []                # remaining first-run steps
        self.from_menu = False
        self.rects = []
        self.wifi_name = None
        self._pending_flow = None
        self.rotation = get_rotation()
        # on a PC the app turns its own picture; on the Pi the system rotates the real screen
        self.app_rotation = 0 if system_rotation_available() else self.rotation
        threading.Thread(target=self._first_run_check, daemon=True).start()

    @property
    def active(self):
        return self.screen is not None

    # -- navigation ---------------------------------------------------------------
    def open_menu(self):
        self.menu_page = 0
        self.rotation = get_rotation()
        self.from_menu = True
        self.flow = []
        self.show("menu")

    def show(self, name):
        self.screen = name
        if name in self.screens:
            self.screens[name].reset()
        if name == "menu":
            self.wifi_changed()

    def done(self):
        """A sub-screen finished (or Back was pressed)."""
        if self.flow:
            self.show(self.flow.pop(0))
        elif self.from_menu and self.screen != "menu":
            self.show("menu")
        else:
            self.screen = None

    def close(self):
        self.screen = None
        self.flow = []

    def wifi_changed(self):
        if not self.wifi.available:
            self.wifi_name = None
            return

        def work():
            try:
                self.wifi_name = self.wifi.current()
            except Exception:
                self.wifi_name = None
        threading.Thread(target=work, daemon=True).start()

    def _rotate(self, step):
        self.rotation = (self.rotation + step) % 360
        set_rotation(self.rotation)
        if not system_rotation_available():
            self.app_rotation = self.rotation

    def _draw_rotate_button(self, ui, cx, cy, clockwise):
        """Round button with a circular arrow (clockwise or anticlockwise)."""
        rect = pygame.Rect(0, 0, 64, 64)
        rect.center = (cx, cy)
        pygame.draw.circle(ui.canvas, SCOPE_BG, (cx, cy), 32)
        pygame.draw.circle(ui.canvas, RING, (cx, cy), 32, 2)
        r = 15
        arc = pygame.Rect(0, 0, 2 * r, 2 * r)
        arc.center = (cx, cy)
        if clockwise:      # gap on the right, arrow head at the top right pointing down
            pygame.draw.arc(ui.canvas, SWEEP, arc, math.radians(40), math.radians(320), 4)
            t = math.radians(40)
            dx, dy = math.sin(t), math.cos(t)
        else:              # mirror image: gap on the left, arrow head at the top left pointing down
            pygame.draw.arc(ui.canvas, SWEEP, arc, math.radians(-140), math.radians(140), 4)
            t = math.radians(140)
            dx, dy = -math.sin(t), -math.cos(t)
        px, py = cx + r * math.cos(t), cy - r * math.sin(t)
        nx, ny = -dy, dx
        tip = (px + dx * 7, py + dy * 7)
        pygame.draw.polygon(ui.canvas, SWEEP, [tip, (px - dx * 3 + nx * 7, py - dy * 3 + ny * 7),
                                               (px - dx * 3 - nx * 7, py - dy * 3 - ny * 7)])
        return rect

    def _cycle_units(self):
        cur = (UNITS["dist"], UNITS["alt"], UNITS["clock"])
        nxt = UNIT_PRESETS[(UNIT_PRESETS.index(cur) + 1) % len(UNIT_PRESETS)] if cur in UNIT_PRESETS else UNIT_PRESETS[0]
        set_units(*nxt)
        save_prefs(units=list(nxt))

    def _cycle_height(self):
        order = [0, 10000, 5000]
        self.max_alt = order[(order.index(self.max_alt) + 1) % 3] if self.max_alt in order else 0
        save_prefs(max_alt_ft=self.max_alt)

    def height_label(self):
        return "All heights" if not self.max_alt else f"Only planes below {fmt_alt_ft(self.max_alt)} (and their alerts)"

    def _cycle_theme(self):
        names = list(THEMES)
        self.theme = names[(names.index(self.theme) + 1) % len(names)]
        save_prefs(theme=self.theme)
        self.ui.apply_theme(self.theme)

    def _start_replay(self):
        self.close()
        self.ui.start_replay()

    def _toggle_text(self):
        self.text_large = not self.text_large
        save_prefs(text_size="large" if self.text_large else "normal")

    def _feed_label(self):
        feed = self.ui.feed
        if not feed or not getattr(feed, "source", ""):
            return "Waiting for the first update"
        age = int(time.time() - feed.stats.get(feed.source, {}).get("ok", time.time()))
        return f"{feed.source}, updated {age} s ago" + (" (rate-limited services resting)" if any(
            t > time.time() for t in feed.resting.values()) else "")

    def _airport_label(self):
        lat, lon = self.location.current()
        near = self.ui.airports.nearest(lat, lon, 1)
        return f"{near[0][0]} {near[0][1]}" if near else "Live arrivals and departures"

    def _page(self, n):
        self.menu_page = n

    def _cycle_night(self):
        order = ["off", "dim", "blank"]
        self.night_mode = order[(order.index(self.night_mode) + 1) % 3] if self.night_mode in order else "dim"
        save_prefs(night_mode=self.night_mode)

    def night_label(self):
        hours = f"{self.cfg.get('night_start', '22:00')}-{self.cfg.get('night_end', '07:00')}"
        return {"off": "Off", "dim": f"Dim at night ({hours})", "blank": f"Screen off at night ({hours})"}.get(self.night_mode, "Off")

    def set_alert_dirs(self, dirs):
        self.alert_dirs = set(dirs)
        save_prefs(alert_directions=[d for d in DIRECTIONS if d in self.alert_dirs])

    def _toggle_pinch(self):
        state = multitouch_state()
        if state is not None:
            set_multitouch(not state)

    @staticmethod
    def _draw_switch(ui, cx, cy, on):
        """Small on/off switch like on a phone."""
        track = pygame.Rect(0, 0, 76, 40)
        track.center = (cx, cy)
        pygame.draw.rect(ui.canvas, SWEEP if on else GRID, track, border_radius=20)
        knob_x = track.right - 20 if on else track.left + 20
        pygame.draw.circle(ui.canvas, (240, 245, 245), (knob_x, cy), 15)

    def _first_run_check(self):
        """No internet at start (after giving the Pi time to join a saved network) -> Wi-Fi setup.
        No location chosen yet -> location search."""
        steps = []
        if self.wifi.available:
            online = False
            for _ in range(8):
                if internet_ok():
                    online = True
                    break
                time.sleep(2)
            if not online:
                steps.append("wifi")
        if not os.path.exists(SAVED_LOCATION):
            steps.append("location")
        if steps:
            self._pending_flow = steps

    # -- per-frame / input --------------------------------------------------------
    def update(self, now):
        if self._pending_flow and self.screen is None:
            self.flow, self._pending_flow = self._pending_flow, None
            self.from_menu = False
            self.show(self.flow.pop(0))
        if self.screen in self.screens:
            self.screens[self.screen].update(now)

    def type_text(self, t):
        if self.screen in self.screens:
            self.screens[self.screen].type_text(t)

    def key(self, k):
        if k == pygame.K_ESCAPE:
            if self.screen == "menu":
                self.close()
            else:
                self.done()
        elif self.screen in self.screens:
            self.screens[self.screen].key(k)

    def tap(self, x, y):
        if self.screen in self.screens:
            self.screens[self.screen].tap(x, y)
            return
        for rect, action in self.rects:
            if rect.collidepoint(x, y):
                self.rects = []
                action()
                return

    def draw(self, now):
        ui = self.ui
        if self.screen in self.screens:
            self.screens[self.screen].draw(ui, now)
            return
        self.rects = []
        draw_screen_bg(ui, "Settings")
        if self.wifi.available:
            wifi_value = f"Connected to {self.wifi_name}" if self.wifi_name else "Not connected"
        else:
            wifi_value = "Managed by this computer"
        touch = multitouch_state()
        if touch is None:
            touch_value = "Not needed on this computer"
        else:
            touch_value = "On" if touch else "Off - touch works like a mouse"
        health, updater = self.ui.health, self.ui.updater
        page = self.menu_page
        if page == 0:
            n_places = len(PlacesScreen.places())
            rows = [("Location", self.location.name, lambda: self.show("location")),
                    ("Saved places", f"{n_places} saved" if n_places else "Save places to switch quickly", lambda: self.show("places")),
                    ("Wi-Fi", wifi_value, lambda: self.show("wifi")),
                    ("Alert directions", describe_directions(self.alert_dirs), lambda: self.show("directions")),
                    ("Night mode", self.night_label(), self._cycle_night)]
        elif page == 1:
            hist = self.ui.feed.history if self.ui.feed else []
            rows = [("Sightings", f"{self.ui.sightings.today()} today, {self.ui.sightings.count_since(7 * 86400)} this week",
                     lambda: self.show("sightings")),
                    ("Airport board", self._airport_label(), lambda: self.show("airport")),
                    ("Traffic", "Aircraft around you over the last 24 hours", lambda: self.show("traffic")),
                    ("Replay", f"Rewind up to {max(1, round((time.time() - hist[0][0]) / 60)) if hist else 0} min"
                     if hist else "Nothing recorded yet", self._start_replay if hist else None)]
        elif page == 2:
            rows = [("Pinch to zoom", touch_value, self._toggle_pinch),
                    ("Screen rotation", f"{self.rotation}\u00b0 clockwise" if self.rotation else "Normal", None),
                    ("Units", units_label(), self._cycle_units),
                    ("Text size", "Large" if self.text_large else "Normal", self._toggle_text),
                    ("Colour theme", THEME_NAMES[self.theme], self._cycle_theme),
                    ("Height filter", self.height_label(), self._cycle_height)]
        else:
            rows = [("Pi health", health.label(), None),
                    ("Crash watchdog", watchdog_label(), None),
                    ("Auto updates", updater.label(), updater.toggle if updater.available else None),
                    ("Feed status", self._feed_label(), lambda: self.show("feed")),
                    ("Offline map", self.ui.map.offline_label() if self.ui.map else "Map is off",
                     self.ui.map.save_offline if self.ui.map else None)]
        titles = ["Settings", "Settings - views", "Settings - display", "Settings - system"]
        draw_screen_bg(ui, titles[page])
        y = 180
        step = min(104, (790 - y) // len(rows))      # more rows -> slightly slimmer rows
        for title, value, action in rows:
            rect = pygame.Rect(0, 0, 700, step - 10)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=14)
            pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=14)
            ui.blit_text(title, 34, TEXT, (rect.x + 28, rect.y + (step - 10) * 0.16), "topleft")
            warn = title == "Pi health" and health.hot()
            ui.blit_text(value[:48], 28, WARN if warn else TEXT_DIM, (rect.x + 28, rect.y + (step - 10) * 0.57), "topleft")
            y += step
            if title == "Screen rotation":
                ccw = self._draw_rotate_button(ui, rect.right - 140, rect.centery, clockwise=False)
                cw = self._draw_rotate_button(ui, rect.right - 60, rect.centery, clockwise=True)
                self.rects.append((ccw, lambda: self._rotate(-90)))
                self.rects.append((cw, lambda: self._rotate(90)))
                continue
            if title in ("Pinch to zoom", "Text size"):
                on = touch if title == "Pinch to zoom" else self.text_large
                if on is not None:
                    self._draw_switch(ui, rect.right - 70, rect.centery, on)
            elif title == "Auto updates":
                if updater.available:
                    self._draw_switch(ui, rect.right - 70, rect.centery, updater.enabled)
            elif action is not None:
                ui.blit_text(">", 44, SWEEP, (rect.right - 40, rect.centery))
            if action is not None:
                self.rects.append((rect, action))
        buttons = []
        if page > 0:
            buttons.append(("< Back", lambda: self._page(page - 1), False))
        buttons.append(("Close", self.close, True))
        if page < 3:
            buttons.append(("More >", lambda: self._page(page + 1), False))
        ui.blit_text(f"Page {page + 1} of 4", 22, TEXT_DIM, (C, y + 4))
        width = 200 if len(buttons) == 3 else 220
        x = C - (len(buttons) * width + (len(buttons) - 1) * 14) // 2
        for label, action, primary in buttons:
            rect = pygame.Rect(x, y + 22, width, 62)
            draw_button(ui, rect, label, primary=primary)
            self.rects.append((rect, action))
            x += width + 14


# --------------------------------------------------------------------------- data feed
class Feed:
    """Polls the aircraft source in a background thread."""

    def __init__(self, cfg, location):
        self.cfg = cfg
        self.location = location
        self.wake = threading.Event()
        location.listeners.append(self.wake.set)
        self.lock = threading.Lock()
        self.aircraft = {}        # hex -> dict (only aircraft with a position)
        self.mil_no_pos = []      # military transponders heard without a position
        self.trails = {}          # hex -> [(lat, lon), ...]
        self.status = "Connecting..."
        self.last_ok = 0.0
        self.resting = {}         # source name -> time until which it asked us to wait
        self.stats = {}           # source name -> {"ok": time, "error": text, "error_at": time, "today": n, "day": date}
        self.source = ""
        self.count = 0
        self.history = []         # [(time, {hex: plane})] for the last 30 minutes (replay)
        self.view = None          # set by the radar: () -> (lat, lon, radius_nm) of what is on screen
        threading.Thread(target=self._run, daemon=True).start()

    def _sources(self):
        """[(name, url)] to try in order: your own receiver, or the free online services in config.json."""
        if self.cfg["source"] == "local":
            return [("own receiver", self.cfg["local_url"])]
        # Planes around the part of the map on screen. Only a rounded point (~10 km) is sent.
        if self.view:
            lat, lon, radius = self.view()
        else:
            (lat, lon), radius = self.location.current(), self.cfg["fetch_radius_nm"]
        out = []
        for template in self.cfg.get("api_urls") or [self.cfg["api_url"]]:
            host = urllib.parse.urlparse(template).hostname or "online feed"
            name = re.sub(r"^(api|opendata)\.", "", host)
            out.append((name, template.format(lat=round(lat, 1), lon=round(lon, 1), radius_nm=min(250, int(radius)))))
        return out

    def _fetch(self):
        """Try each source until one answers. A source that says "too many requests" (HTTP 429)
        is rested for as long as it asks, and the next source is used meanwhile."""
        problems, now = [], time.time()
        for name, url in self._sources():
            if self.resting.get(name, 0) > now:
                problems.append(f"{name} rate-limited")
                continue
            st = self.stats.setdefault(name, {"ok": 0, "error": "", "error_at": 0, "today": 0, "day": ""})
            day = time.strftime("%Y-%m-%d")
            if st["day"] != day:
                st["day"], st["today"] = day, 0
            st["today"] += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.load(resp)
                st["ok"], st["error"] = time.time(), ""
                return name, data
            except urllib.error.HTTPError as e:
                st["error"], st["error_at"] = ("rate-limited" if e.code == 429 else f"HTTP {e.code}"), time.time()
                if e.code == 429:
                    wait = e.headers.get("Retry-After") or e.headers.get("x-ratelimit-reset") or "600"
                    wait = int(wait) if str(wait).isdigit() else 600
                    self.resting[name] = now + min(max(wait, 60), 6 * 3600)
                    problems.append(f"{name} rate-limited")
                else:
                    problems.append(f"{name} HTTP {e.code}")
            except Exception as e:
                st["error"], st["error_at"] = type(e).__name__, time.time()
                problems.append(f"{name} {type(e).__name__}")
        raise RuntimeError(", ".join(problems))

    def _run(self):
        while True:
            try:
                source, data = self._fetch()
                planes = data.get("ac") or data.get("aircraft") or []
                self.source, self.count = source, len(planes)
                keep = ("hex", "flight", "r", "t", "category", "dbFlags", "lat", "lon", "alt_baro", "alt_geom",
                        "gs", "track", "squawk")
                snap = {pl["hex"].lower(): {k: pl[k] for k in keep if k in pl}
                        for pl in planes if pl.get("hex") and "lat" in pl and "lon" in pl}
                with self.lock:
                    self.history.append((time.time(), snap))
                    cutoff = time.time() - 30 * 60
                    while self.history and self.history[0][0] < cutoff:
                        self.history.pop(0)
                positioned, mil_no_pos = {}, []
                for p in planes:
                    hx = p.get("hex", "").lower()
                    if not hx:
                        continue
                    if "lat" in p and "lon" in p:
                        positioned[hx] = p
                    elif is_military(p):
                        mil_no_pos.append(p)
                with self.lock:
                    for hx, p in positioned.items():
                        trail = self.trails.setdefault(hx, [])
                        pos = (p["lat"], p["lon"])
                        if not trail or trail[-1] != pos:
                            trail.append(pos)
                            del trail[:-40]
                    for hx in [h for h in self.trails if h not in positioned]:
                        del self.trails[hx]
                    self.aircraft = positioned
                    self.mil_no_pos = mil_no_pos
                    self.status = "Live: " + source
                    self.last_ok = time.time()
            except Exception as e:  # network hiccups should never kill the display
                with self.lock:
                    self.status = f"No flight data ({str(e)[:60]}), retrying"
            self.wake.wait(self.cfg["poll_seconds"])   # wakes early when the location changes
            self.wake.clear()

    def history_at(self, t):
        """(aircraft at time t, trails for the 3 minutes before it) from the replay history."""
        with self.lock:
            hist = list(self.history)
        if not hist:
            return {}, {}
        times = [h[0] for h in hist]
        i = max(0, bisect.bisect_right(times, t) - 1)
        aircraft = {k: dict(v) for k, v in hist[i][1].items()}
        trails = {}
        for ts, snap in hist[max(0, i - 36):i + 1]:
            for hx, pl in snap.items():
                if hx in aircraft:
                    trails.setdefault(hx, []).append((pl["lat"], pl["lon"]))
        return aircraft, trails

    def snapshot(self):
        with self.lock:
            return dict(self.aircraft), list(self.mil_no_pos), {k: list(v) for k, v in self.trails.items()}, self.status, self.last_ok


class MapLayer:
    """Dark street map under the radar, built from cached web map tiles.

    Tiles are downloaded once into map_tiles/ and reused offline after that.
    Each background is pre-rendered larger than the screen, so dragging the map stays smooth;
    a new one is built in the background once you move close to its edge.
    """
    TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    EARTH_KM = 40075.017
    SIZE = int(CANVAS * 1.6)

    def __init__(self, cfg, location):
        self.cfg = cfg
        self.location = location
        self.alpha = int(255 * cfg.get("map_brightness", 0.6))
        self.cache_dir = os.path.join(HERE, "map_tiles", "osm")
        self.lock = threading.Lock()
        self.surfaces = {}        # range_km -> (surface, (lat, lon) at its centre); only the last few kept
        self.wanted = {}          # range_km -> centre to build next
        self.pending = set()
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _queue(self, r):
        if r not in self.pending:
            self.pending.add(r)
            self.jobs.put(r)

    def get(self, range_km, center):
        """(surface, its centre) for this zoom, or None. Asks for a rebuild when the view nears its edge."""
        spare_km = (self.SIZE - CANVAS) / 2 / (R / range_km)
        with self.lock:
            entry = self.surfaces.get(range_km)
            if entry is None or km_between(entry[1], center) > spare_km * 0.5:
                self.wanted[range_km] = center
                self._queue(range_km)
            return entry

    def nearest(self, range_km):
        """The built map closest to this zoom, as (surface, centre, its range) — used while pinching
        or while the exact zoom is still being built."""
        with self.lock:
            if not self.surfaces:
                return None
            r = min(self.surfaces, key=lambda k: abs(math.log(k / range_km)))
            surf, center = self.surfaces[r]
            return surf, center, r

    def _worker(self):
        while True:
            r = self.jobs.get()
            with self.lock:
                center = self.wanted.get(r)
            if center is None:
                with self.lock:
                    self.pending.discard(r)
                continue
            try:
                surf, missing = self._build(r, center)
                with self.lock:
                    self.surfaces.pop(r, None)
                    self.surfaces[r] = (surf, center)
                    while len(self.surfaces) > 3:          # keep memory low on the Pi
                        del self.surfaces[next(iter(self.surfaces))]
            except Exception:
                missing = 1
            with self.lock:
                self.pending.discard(r)
            if missing:   # offline or a tile failed: try this range again later
                threading.Timer(60, self._retry, args=(r,)).start()

    def offline_label(self):
        st = getattr(self, "offline", None)
        if not st:
            return "Tap to save the map around home for offline use"
        if st["state"] == "saving":
            return f"Saving... {st['done']} of {st['total']} tiles"
        return f"Saved: {st['done']} of {st['total']} tiles (home view, every zoom)" + (
            f", {st['failed']} failed" if st["failed"] else "")

    def save_offline(self):
        """Download the tiles for the home view at every zoom step. Kept modest on purpose: OpenStreetMap's
        tile policy doesn't allow bulk-downloading whole regions."""
        if getattr(self, "offline", None) and self.offline["state"] == "saving":
            return
        center = self.location.current()
        jobs = set()
        for r in self.cfg["ranges_km"]:
            lat, lon = center
            lat_r = math.radians(lat)
            want = R / r
            z = max(3, min(19, round(math.log2(want * self.EARTH_KM * math.cos(lat_r) / 256))))
            n = 2 ** z
            f = want / (256 * n / (self.EARTH_KM * math.cos(lat_r)))
            wx = (lon + 180) / 360 * 256 * n
            wy = (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * 256 * n
            hw = C / f                     # just what the round screen shows at home
            for tx in range(int((wx - hw) // 256), int((wx + hw) // 256) + 1):
                for ty in range(int((wy - hw) // 256), int((wy + hw) // 256) + 1):
                    jobs.add((z, tx % n, ty))
        self.offline = {"state": "saving", "done": 0, "total": len(jobs), "failed": 0}

        def work():
            for z, x, y in sorted(jobs):
                try:
                    self._tile(z, x, y)
                except Exception:
                    self.offline["failed"] += 1
                else:
                    self.offline["done"] += 1
            self.offline["state"] = "done"
        threading.Thread(target=work, daemon=True).start()

    def _retry(self, r):
        with self.lock:
            self._queue(r)

    def _tile(self, z, x, y):
        path = os.path.join(self.cache_dir, str(z), str(x), f"{y}.png")
        if not os.path.exists(path):
            url = self.TILE_URL.format(z=z, x=x, y=y)
            req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1 (personal display)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        return pygame.image.load(path)

    def _build(self, range_km, center):
        lat, lon = center
        lat_r = math.radians(lat)
        want = R / range_km                                   # canvas px per km
        km_per_world_px0 = self.EARTH_KM * math.cos(lat_r) / 256
        z = max(3, min(19, round(math.log2(want * km_per_world_px0))))
        n = 2 ** z
        f = want / (256 * n / (self.EARTH_KM * math.cos(lat_r)))  # tile px -> canvas px
        wx = (lon + 180) / 360 * 256 * n
        wy = (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * 256 * n
        hw = self.SIZE / 2 / f
        x0, y0, x1, y1 = wx - hw, wy - hw, wx + hw, wy + hw
        tx0, ty0, tx1, ty1 = int(x0 // 256), int(y0 // 256), int(x1 // 256), int(y1 // 256)
        big = pygame.Surface(((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
        big.fill((242, 239, 233))   # OSM land colour, so a missing tile turns dark, not bright
        missing = 0
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                try:
                    big.blit(self._tile(z, tx % n, ty), ((tx - tx0) * 256, (ty - ty0) * 256))
                except Exception:
                    missing += 1
        crop = big.subsurface(pygame.Rect(int(x0 - tx0 * 256), int(y0 - ty0 * 256), int(2 * hw), int(2 * hw)))
        size = self.SIZE
        scaled = pygame.transform.smoothscale(crop, (size, size))
        # Turn the light OSM map into a dark "night radar" map: invert, then tint teal.
        night = pygame.Surface((size, size))
        night.fill((255, 255, 255))
        night.blit(scaled, (0, 0), special_flags=pygame.BLEND_RGB_SUB)
        night.fill(MAP_TINT, special_flags=pygame.BLEND_RGB_MULT)
        night.set_alpha(self.alpha)
        final = pygame.Surface((size, size))   # opaque, so drawing it each frame is cheap
        final.fill(SCOPE_BG)
        final.blit(night, (0, 0))
        return final, missing


UNITS = {"dist": "km", "alt": "ft", "clock": "24h"}
UNIT_PRESETS = [("km", "ft", "24h"), ("km", "m", "24h"), ("mi", "ft", "12h"), ("mi", "ft", "24h")]


def set_units(dist, alt, clock):
    UNITS.update(dist=dist, alt=alt, clock=clock)


def units_label():
    return f"{UNITS['dist']}, {UNITS['alt']}, {UNITS['clock'].replace('h', ' h')} clock"


def fmt_dist(km):
    """Distance in the chosen units: '1.5 km' / '50 m', or '2.3 mi' / '800 ft'."""
    if UNITS["dist"] == "mi":
        miles = km * 0.621371
        if miles < 0.5:
            return f"{int(round(km * 3280.84 / 10) * 10):,} ft"
        return f"{round(miles, 1 if miles < 10 else 0):g} mi"
    if km < 1:
        return f"{round(km * 1000):g} m"
    return f"{km:g} km"


def fmt_alt_ft(ft):
    if UNITS["alt"] == "m":
        return f"{int(round(ft * 0.3048)):,} m"
    return f"{int(ft):,} ft"


def fmt_clock(ts=None):
    lt = time.localtime(ts if ts is not None else time.time())
    if UNITS["clock"] == "12h":
        return time.strftime("%I:%M %p", lt).lstrip("0")
    return time.strftime("%H:%M", lt)


def is_military(p):
    return bool(p.get("dbFlags", 0) & 1)


def callsign(p):
    return (p.get("flight") or "").strip() or p.get("r") or p.get("hex", "?").upper()


def fmt_alt(p):
    alt = p.get("alt_baro", p.get("alt_geom"))
    if alt == "ground":
        return "ground"
    if isinstance(alt, (int, float)):
        return fmt_alt_ft(alt)
    return "?"


SIZE_FACTOR = {"four": 1.5, "jet": 1.0, "fighter": 0.75, "heli": 0.55, "light": 0.5}   # how big it looks
NOISE_AT_300M = {"fighter": 108, "four": 98, "jet": 92, "heli": 88, "light": 78}      # rough dB, 300 m away


def loudness_words(db):
    for limit, words in ((85, "very loud"), (75, "loud"), (62, "audible"), (50, "faint")):
        if db >= limit:
            return words
    return "silent"


# --------------------------------------------------------------------------- aircraft details
ADSBDB_URL = "https://api.adsbdb.com/v0"
TRACK_URL = "https://opensky-network.org/api/tracks/all"

# Friendly names for common ICAO type codes (anything missing falls back to the online database).
TYPE_NAMES = {
    # fighters and combat
    "EUFI": "Eurofighter Typhoon", "RFAL": "Dassault Rafale", "F35": "F-35 Lightning II", "F22": "F-22 Raptor",
    "F16": "F-16 Fighting Falcon", "F15": "F-15 Eagle", "F15E": "F-15E Strike Eagle", "F18": "F/A-18 Hornet",
    "F18H": "F/A-18 Hornet", "F18S": "F/A-18 Super Hornet", "TOR": "Panavia Tornado", "GRIF": "Saab Gripen",
    "JAS39": "Saab Gripen", "MIR2": "Dassault Mirage 2000", "A10": "A-10 Thunderbolt II", "B52": "B-52 Stratofortress",
    "B1": "B-1 Lancer", "B2": "B-2 Spirit", "HAWK": "BAE Hawk", "M346": "Leonardo M-346", "PC21": "Pilatus PC-21",
    "T38": "T-38 Talon", "AJET": "Alpha Jet",
    # military transport, tanker, surveillance
    "A400": "Airbus A400M Atlas", "C130": "C-130 Hercules", "C30J": "C-130J Super Hercules", "C17": "C-17 Globemaster III",
    "C5M": "C-5M Super Galaxy", "K35R": "KC-135 Stratotanker", "KC46": "KC-46 Pegasus", "A332": "Airbus A330-200",
    "E3TF": "E-3 Sentry (AWACS)", "E3CF": "E-3 Sentry (AWACS)", "P8": "P-8 Poseidon", "R135": "RC-135",
    "GLF5": "Gulfstream G550", "Q4": "RQ-4 Global Hawk", "MQ9": "MQ-9 Reaper", "C295": "Airbus C295",
    "CN35": "CASA CN-235", "C160": "Transall C-160", "V22": "V-22 Osprey", "H60": "Black Hawk helicopter",
    "NH90": "NH90 helicopter", "H47": "CH-47 Chinook", "EC35": "Airbus H135 helicopter", "EC45": "Airbus H145 helicopter",
    "TIGR": "Eurocopter Tiger", "LYNX": "Westland Lynx", "A139": "Leonardo AW139",
    # airliners
    "A19N": "Airbus A319neo", "A319": "Airbus A319", "A320": "Airbus A320", "A20N": "Airbus A320neo",
    "A321": "Airbus A321", "A21N": "Airbus A321neo", "A318": "Airbus A318", "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo", "A339": "Airbus A330-900neo", "A343": "Airbus A340-300", "A346": "Airbus A340-600",
    "A359": "Airbus A350-900", "A35K": "Airbus A350-1000", "A388": "Airbus A380", "BCS1": "Airbus A220-100",
    "BCS3": "Airbus A220-300", "B737": "Boeing 737-700", "B738": "Boeing 737-800", "B739": "Boeing 737-900",
    "B37M": "Boeing 737 MAX 7", "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9", "B744": "Boeing 747-400",
    "B748": "Boeing 747-8", "B752": "Boeing 757-200", "B763": "Boeing 767-300", "B772": "Boeing 777-200",
    "B77L": "Boeing 777-200LR", "B77W": "Boeing 777-300ER", "B788": "Boeing 787-8", "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10", "E170": "Embraer 170", "E75L": "Embraer 175", "E190": "Embraer 190",
    "E195": "Embraer 195", "E290": "Embraer E190-E2", "E295": "Embraer E195-E2", "CRJ7": "Bombardier CRJ700",
    "CRJ9": "Bombardier CRJ900", "CRJX": "Bombardier CRJ1000", "AT75": "ATR 72-500", "AT76": "ATR 72-600",
    "AT45": "ATR 42-500", "DH8D": "Dash 8 Q400", "SB20": "Saab 2000", "MD11": "McDonnell Douglas MD-11",
    "C56X": "Cessna Citation Excel", "C68A": "Cessna Citation Latitude", "PC12": "Pilatus PC-12",
    "C172": "Cessna 172", "DA42": "Diamond DA42", "F2TH": "Dassault Falcon 2000", "BE20": "Beechcraft King Air 200",
}

# Military callsign prefixes -> who flies them (used when the database has no owner).
MIL_OPERATORS = {
    "GAF": "German Air Force", "GNY": "German Navy", "GAM": "German Army", "NATO": "NATO", "RCH": "US Air Force",
    "RRR": "Royal Air Force", "ASCOT": "Royal Air Force", "CTM": "French Air Force", "FAF": "French Air Force",
    "IAM": "Italian Air Force", "BAF": "Belgian Air Force", "NAF": "Netherlands Air Force", "SUI": "Swiss Air Force",
    "PLF": "Polish Air Force", "HAF": "Hellenic Air Force", "CFC": "Canadian Forces", "SVF": "Swedish Air Force",
    "DUKE": "US Army", "SPAR": "US Air Force", "LAGR": "US Air Force", "HOBO": "US Air Force", "JAKE": "US Navy",
}


def model_name(type_code, db):
    if type_code and type_code in TYPE_NAMES:
        return TYPE_NAMES[type_code]
    if db:
        maker, kind = (db.get("manufacturer") or "").strip(), (db.get("type") or "").strip()
        if kind:
            return kind if kind.lower().startswith(maker.lower()) else f"{maker} {kind}".strip()
    return type_code or None


def military_operator(callsign):
    cs = (callsign or "").upper()
    for prefix in sorted(MIL_OPERATORS, key=len, reverse=True):
        if cs.startswith(prefix):
            return MIL_OPERATORS[prefix]
    return None


def fmt_duration(hours):
    mins = int(round(hours * 60))
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60:02d} min"


def great_circle_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def great_circle_points(lat1, lon1, lat2, lon2, step_km=10):
    """Points along the shortest route over the globe (what a plane actually flies)."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return [(lat1, lon1)]
    n = max(2, min(400, int(d * 6371.0 / step_km)))
    pts = []
    for i in range(n + 1):
        f = i / n
        a, b = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        pts.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return pts


class AircraftInfo:
    """Looks up model, airline/operator and route for the tapped plane.

    Only runs for the plane you tap, and caches the answers, so very few requests are made.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.jobs = queue.Queue()
        self.tracks = {}              # hex -> {"time", "points"}
        self.track_jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._track_worker, daemon=True).start()

    def track(self, p):
        """Flown path of this flight from OpenSky, as [(lat, lon), ...]; refreshed every 2 minutes."""
        hx = p.get("hex", "").lower()
        with self.lock:
            entry = self.tracks.get(hx)
            if entry is None or (time.time() - entry["time"] > 120 and not entry.get("pending")):
                if len(self.tracks) > 100:
                    self.tracks.clear()
                entry = self.tracks.setdefault(hx, {"time": 0.0, "points": None})
                entry["pending"] = True
                self.track_jobs.put(hx)
            return entry["points"]

    def _track_worker(self):
        while True:
            hx = self.track_jobs.get()
            points = None
            try:
                data = self._fetch(f"{TRACK_URL}?icao24={hx}&time=0") or {}
                points = [(pt[1], pt[2]) for pt in data.get("path") or [] if pt[1] is not None and pt[2] is not None]
            except Exception:
                pass                      # not tracked by OpenSky or offline: the live trail is used instead
            with self.lock:
                old = self.tracks.get(hx, {}).get("points")
                self.tracks[hx] = {"time": time.time(), "points": points or old, "pending": False}

    def get(self, p):
        key = (p.get("hex", "").lower(), (p.get("flight") or "").strip())
        with self.lock:
            info = self.cache.get(key)
            if info is None or (info["state"] == "error" and time.time() - info["time"] > 60):
                if len(self.cache) > 300:
                    self.cache.clear()
                info = self.cache[key] = {"state": "loading", "time": time.time(), "model": None, "operator": None,
                                          "origin": None, "dest": None}
                self.jobs.put((key, p.get("t"), is_military(p), p.get("r")))
            return dict(info)

    @staticmethod
    def _fetch(url, raw=False):
        req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1 (personal aircraft display)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read() if raw else json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None           # simply not in the database
            raise

    @staticmethod
    def _place(a):
        if not a:
            return None
        return {"code": a.get("iata_code") or a.get("icao_code") or "?",
                "city": a.get("municipality") or a.get("name") or "",
                "lat": a.get("latitude"), "lon": a.get("longitude")}

    def _worker(self):
        while True:
            (hx, cs), type_code, mil, reg = self.jobs.get()
            out, state = {}, "done"
            try:
                db = ((self._fetch(f"{ADSBDB_URL}/aircraft/{hx}") or {}).get("response") or {}).get("aircraft")
                if not isinstance(db, dict):
                    db = None
                out["model"] = model_name(type_code, db)
                reg = reg or (db or {}).get("registration")
                if mil:
                    out["operator"] = (db or {}).get("registered_owner") or military_operator(cs)
                else:
                    route = None
                    if cs:
                        route = ((self._fetch(f"{ADSBDB_URL}/callsign/{urllib.parse.quote(cs)}") or {})
                                 .get("response") or {}).get("flightroute")
                    if isinstance(route, dict):
                        out["operator"] = (route.get("airline") or {}).get("name")
                        out["origin"] = self._place(route.get("origin"))
                        out["dest"] = self._place(route.get("destination"))
                    out["operator"] = out.get("operator") or (db or {}).get("registered_owner")
            except Exception:
                state = "error"           # offline or rate-limited: tried again after a minute
            with self.lock:
                if (hx, cs) in self.cache:
                    self.cache[(hx, cs)].update(out, state=state, time=time.time())


# --------------------------------------------------------------------------- sun
def sun_position(lat, lon, ts):
    """(azimuth, elevation) of the sun in degrees for a place and time (accurate to well under a degree)."""
    d = ts / 86400.0 + 2440587.5 - 2451545.0                 # days since 1 Jan 2000, 12:00 UTC
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians((gmst * 15 + lon) % 360) - ra
    phi = math.radians(lat)
    el = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(phi) - math.sin(phi) * math.cos(ha))
    return math.degrees(az) % 360, math.degrees(el)


def angle_between(az1, el1, az2, el2):
    """Angle in degrees between two directions in the sky."""
    a1, e1, a2, e2 = map(math.radians, (az1, el1, az2, el2))
    c = math.sin(e1) * math.sin(e2) + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# --------------------------------------------------------------------------- Pi health
class PiHealth:
    """Raspberry Pi temperature, cooler fan speed and throttling, read every 10 seconds."""
    TEMP_FILE = "/sys/class/thermal/thermal_zone0/temp"
    HOT_C = 75

    def __init__(self):
        self.available = os.path.exists(self.TEMP_FILE)
        self.temp = self.fan_rpm = None
        self.throttled = 0
        if self.available:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import glob
        while True:
            try:
                with open(self.TEMP_FILE) as f:
                    self.temp = int(f.read().strip()) / 1000
            except (OSError, ValueError):
                pass
            for path in glob.glob("/sys/class/hwmon/hwmon*/fan1_input"):
                try:
                    with open(path) as f:
                        self.fan_rpm = int(f.read().strip())
                    break
                except (OSError, ValueError):
                    pass
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
                self.throttled = int(out.strip().split("=")[1], 16)
            except Exception:
                pass
            time.sleep(10)

    def hot(self):
        """True when the Pi is too warm or is slowing itself down right now."""
        return (self.temp is not None and self.temp >= self.HOT_C) or bool(self.throttled & 0b110)

    def label(self):
        if not self.available:
            return "Only on the Raspberry Pi"
        if self.temp is None:
            return "Reading..."
        parts = [f"CPU {self.temp:.0f}\u00b0C"]
        if self.fan_rpm is not None:
            parts.append(f"fan {self.fan_rpm} rpm" if self.fan_rpm else "fan off")
        if self.throttled & 0x1:
            parts.append("LOW POWER - use a 27 W supply")
        elif self.throttled & 0b110:
            parts.append("SLOWED DOWN - too hot")
        elif self.temp >= self.HOT_C:
            parts.append("HOT")
        else:
            parts.append("OK")
        return ", ".join(parts)


# --------------------------------------------------------------------------- automatic updates
class AutoUpdater:
    """Optional nightly update from GitHub (Raspberry Pi only). Runs run.py --update at 03:00 when there is
    something new, never while an alert is on screen."""
    HOUR = 3

    def __init__(self, radar):
        self.radar = radar
        self.available = sys.platform.startswith("linux") and os.path.isdir(os.path.join(HERE, ".git")) \
            and shutil.which("git") is not None
        prefs = load_prefs()
        self.enabled = bool(prefs.get("auto_update", False))
        self.last = prefs.get("auto_update_last", "")
        if self.available:
            threading.Thread(target=self._run, daemon=True).start()

    def toggle(self):
        self.enabled = not self.enabled
        save_prefs(auto_update=self.enabled)

    def label(self):
        if not self.available:
            return "Only on the Raspberry Pi"
        if not self.enabled:
            return "Off"
        return f"On, nightly at {self.HOUR:02d}:00" + (f" - {self.last}" if self.last else "")

    def _note(self, text):
        self.last = text
        save_prefs(auto_update_last=text)

    def _run(self):
        done_day = None
        while True:
            time.sleep(60)
            lt = time.localtime()
            today = time.strftime("%Y-%m-%d", lt)
            if not self.enabled or lt.tm_hour != self.HOUR or done_day == today or self.radar.alert_hex:
                continue
            done_day = today
            try:
                subprocess.run(["git", "-C", HERE, "fetch", "-q"], timeout=120, check=True)
                local = subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                remote = subprocess.run(["git", "-C", HERE, "rev-parse", "@{u}"], capture_output=True, text=True).stdout.strip()
                if local == remote:
                    self._note(f"up to date ({time.strftime('%d %b', lt)})")
                    continue
                self._note(f"updated {time.strftime('%d %b', lt)}")
                subprocess.Popen([sys.executable, os.path.join(HERE, "run.py"), "--update"], cwd=HERE,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)       # restarts Sky Radar with the new version
            except Exception as e:
                self._note(f"check failed ({type(e).__name__})")


# --------------------------------------------------------------------------- weather
class Weather:
    """Cloud cover at home from Open-Meteo, refreshed every 15 minutes (position rounded to ~1 km)."""

    def __init__(self, location):
        self.location = location
        self.cloud = None          # percent
        self.visibility_km = None
        self.contrails = ""        # e.g. "long trails likely"
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            lat, lon = self.location.current()
            try:
                url = f"{WEATHER_URL}?latitude={lat:.2f}&longitude={lon:.2f}&current=cloud_cover,visibility"
                req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cur = json.load(resp).get("current", {})
                self.cloud = cur.get("cloud_cover")
                vis = cur.get("visibility")
                self.visibility_km = vis / 1000 if isinstance(vis, (int, float)) else None
                self.contrails = self._contrails(lat, lon)
            except Exception:
                pass                       # keep the last value; try again later
            time.sleep(15 * 60)

    @staticmethod
    def _contrails(lat, lon):
        """Contrail outlook at cruising height (250/300 hPa, about 34,000 / 30,000 ft): trails form when it's
        colder than about -40 C, and last long when the air is saturated with respect to ice."""
        url = (f"{WEATHER_URL}?latitude={lat:.2f}&longitude={lon:.2f}&forecast_days=1&timezone=auto&hourly="
               "temperature_250hPa,relative_humidity_250hPa,temperature_300hPa,relative_humidity_300hPa")
        req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            h = json.load(resp)["hourly"]
        i = min(time.localtime().tm_hour, len(h["time"]) - 1)
        best = "none"
        for level in ("250", "300"):
            t, rhw = h[f"temperature_{level}hPa"][i], h[f"relative_humidity_{level}hPa"][i]
            if t is None or rhw is None or t > -40:
                continue
            e_water = 6.112 * math.exp(17.62 * t / (243.12 + t))
            e_ice = 6.112 * math.exp(22.46 * t / (272.62 + t))
            rh_ice = rhw * e_water / e_ice
            best = "long" if rh_ice >= 100 else ("short" if best == "none" else best)
        return {"long": "long trails likely", "short": "short trails only", "none": "no trails expected"}[best]

    def label(self):
        return f"clouds {round(self.cloud)}%" if isinstance(self.cloud, (int, float)) else ""

    def spotting_hint(self):
        """Short warning for the alert banner when the sky is unlikely to cooperate."""
        if isinstance(self.cloud, (int, float)) and self.cloud >= 80:
            return f"Cloudy ({round(self.cloud)}%) - it may be hidden"
        if isinstance(self.visibility_km, (int, float)) and self.visibility_km < 3:
            return f"Poor visibility ({self.visibility_km:.1f} km)"
        return ""


# --------------------------------------------------------------------------- sightings log
class Sightings:
    """Every military aircraft that came within sightings_km of home, one entry per aircraft per day.
    Kept for 90 days in sightings.json on this device (git-ignored, never uploaded)."""

    def __init__(self, cfg):
        self.radius = cfg.get("sightings_km", 10)
        self.dirty, self.saved_at = False, 0.0
        try:
            with open(SIGHTINGS_FILE, encoding="utf-8") as f:
                self.entries = json.load(f)
        except (OSError, ValueError):
            self.entries = []
        cutoff = time.time() - 90 * 86400
        self.entries = [e for e in self.entries if e.get("last", 0) >= cutoff]

    def record(self, p, model, direction, now):
        if p.get("_dist_km", 99) > self.radius or p.get("alt_baro") == "ground":
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        for e in reversed(self.entries):
            if e["hex"] == p.get("hex") and e["day"] == day:
                break
        else:
            e = {"hex": p.get("hex"), "day": day, "first": now, "closest_km": 99}
            self.entries.append(e)
        e["last"] = now
        e["callsign"] = callsign(p)
        e["model"] = model or e.get("model") or p.get("t") or "Unknown type"
        if p["_dist_km"] < e["closest_km"]:
            e["closest_km"] = round(p["_dist_km"], 2)
            e["dir"] = direction
            e["alt"] = fmt_alt(p)
        alt = p.get("alt_baro")
        if isinstance(alt, (int, float)) and alt < e.get("min_alt_ft", 10 ** 6):
            e["min_alt_ft"] = int(alt)
        self.dirty = True

    def maybe_save(self, now):
        if self.dirty and now - self.saved_at > 30:
            try:
                with open(SIGHTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.entries, f, indent=1)
                self.dirty, self.saved_at = False, now
            except OSError:
                pass

    def count_since(self, seconds):
        cutoff = time.time() - seconds
        return sum(1 for e in self.entries if e.get("last", 0) >= cutoff)

    def today(self):
        day = time.strftime("%Y-%m-%d")
        return sum(1 for e in self.entries if e["day"] == day)

    def best_times(self):
        """grid[weekday 0=Mon][2-hour slot 0..11] = number of sightings."""
        grid = [[0] * 12 for _ in range(7)]
        for e in self.entries:
            lt = time.localtime(e.get("first", 0))
            grid[lt.tm_wday][lt.tm_hour // 2] += 1
        return grid

    def records(self):
        """[(label, text)] for the Records page."""
        if not self.entries:
            return []
        out = []
        c = min(self.entries, key=lambda e: e.get("closest_km", 99))
        out.append(("Closest pass", f"{c.get('callsign')} {c.get('model')}, {fmt_dist(c.get('closest_km', 0))} {c.get('dir', '')}, "
                                    + time.strftime("%d %b", time.localtime(c.get("first", 0)))))
        with_alt = [e for e in self.entries if isinstance(e.get("min_alt_ft"), int)]
        if with_alt:
            lo = min(with_alt, key=lambda e: e["min_alt_ft"])
            out.append(("Lowest", f"{lo.get('callsign')} {lo.get('model')}, {fmt_alt_ft(lo['min_alt_ft'])}, "
                                  + time.strftime("%d %b", time.localtime(lo.get("first", 0)))))
        types = {}
        for e in self.entries:
            types[e.get("model", "?")] = types.get(e.get("model", "?"), 0) + 1
        top = max(types.items(), key=lambda kv: kv[1])
        out.append(("Most seen", f"{top[0]} ({top[1]}x)"))
        days = {}
        for e in self.entries:
            days[e["day"]] = days.get(e["day"], 0) + 1
        busiest = max(days.items(), key=lambda kv: kv[1])
        out.append(("Busiest day", time.strftime("%a %d %b", time.strptime(busiest[0], "%Y-%m-%d")) + f" ({busiest[1]} jets)"))
        out.append(("Total", f"{len(self.entries)} sightings in {len(days)} days with jets (last 90 days)"))
        return out


# --------------------------------------------------------------------------- radar view
class Radar:
    def __init__(self, cfg, location):
        self.cfg = cfg
        self.location = location
        self.home_lat, self.home_lon = location.current()
        self.view_lat, self.view_lon = self.home_lat, self.home_lon   # centre of the screen
        self._update_view()
        location.listeners.append(self.recenter)                       # new location chosen: jump there
        location.listeners.append(self._start_elevation_lookup)        # ...and look up its ground height
        self._start_elevation_lookup()
        self._press = None
        self._last_drag = None
        self.dragging = False
        self._feed_key = None
        self._feed_wake = 0.0
        self.fingers = {}                 # touch id -> canvas (x, y)
        self.pinch = None
        self.live_range = None            # smooth zoom level while pinching
        self.ranges = cfg["ranges_km"]
        start = cfg.get("start_range_km", self.ranges[0])
        self.range_idx = self.ranges.index(start) if start in self.ranges else 0
        self.selected = None
        self.canvas = pygame.Surface((CANVAS, CANVAS))
        self.fonts = {s: pygame.font.Font(None, s) for s in (20, 22, 24, 28, 34, 44)}
        self.text_scale = 1.0
        self.feed = None
        self._last_empty_tap = (0.0, 0.0, 0.0)
        self._text_cache = {}
        self.btn_minus = (95, C)
        self.btn_plus = (CANVAS - 95, C)
        self.btn_map = (95, C + 110)
        self.btn_settings = (CANVAS - 95, C + 110)
        self.btn_home = (CANVAS - 95, C - 110)
        self.screen_pos = {}      # hex -> canvas (x, y), for tap hit-testing
        self.map = MapLayer(cfg, location) if cfg.get("map", True) else None
        self.show_map = self.map is not None
        self.sweep_surf = pygame.Surface((CANVAS, CANVAS), pygame.SRCALPHA)
        self.sightings = Sightings(cfg)
        self.settings = Settings(self, location, cfg)
        self.info = AircraftInfo()
        self.weather = Weather(location)
        self.health = PiHealth()
        self.airports = Airports()
        self.replay = None                     # {"t": time shown, "playing": bool, "last": frame time}
        self.replay_rects = []
        try:
            with open(os.path.join(HERE, "traffic.json"), encoding="utf-8") as f:
                self.traffic = [tuple(x) for x in json.load(f)]
        except (OSError, ValueError):
            self.traffic = []
        self._traffic_at = 0.0
        self.apply_theme(self.settings.theme)
        self.updater = AutoUpdater(self)
        self.auto = None                       # auto-zoom state while an alert is on
        self.wake_until = 0.0                 # night mode: a touch wakes the screen for a minute
        self._swallow = False
        self._mil_alert = False
        self._night_fill = pygame.Surface((CANVAS, CANVAS))
        self._night_fill.fill(BLACK)
        self.card_rect = None
        self.card_collapsed = False
        self._outside = pygame.Surface((CANVAS, CANVAS))   # black everywhere except the round screen
        self._outside.fill(BLACK)
        pygame.draw.circle(self._outside, (255, 0, 255), (C, C), C)
        self._outside.set_colorkey((255, 0, 255))
        self._halo = pygame.Surface((64, 64), pygame.SRCALPHA)       # soft halo around the blue dot
        self._alert_fill = {}
        for name, col in (("red", ALERT_RED), ("yellow", ALERT_YELLOW)):
            surf = pygame.Surface((CANVAS, CANVAS))
            surf.fill(col)
            self._alert_fill[name] = surf
        self.alert_rect = None
        self.alert_hex = None
        pygame.draw.circle(self._halo, (*HOME_BLUE, 60), (32, 32), 30)

    @property
    def range_km(self):
        return self.live_range or self.ranges[self.range_idx]

    def apply_theme(self, name):
        set_theme(name)
        self._text_cache.clear()
        self._halo = pygame.Surface((64, 64), pygame.SRCALPHA)
        pygame.draw.circle(self._halo, (*HOME_BLUE, 60), (32, 32), 30)
        if self.map:
            with self.map.lock:
                self.map.surfaces.clear()          # rebuilt in the new colours

    def start_replay(self):
        if self.feed and self.feed.history:
            oldest = self.feed.history[0][0]
            self.replay = {"t": max(oldest, time.time() - 10 * 60), "playing": False, "last": time.time()}

    def spotting(self, p, dist_km):
        """("Easy to see" ... "Not visible", "silent" ... "very loud") for a plane at this distance from you."""
        alt = p.get("alt_geom") if isinstance(p.get("alt_geom"), (int, float)) else p.get("alt_baro")
        if not isinstance(alt, (int, float)):
            return "", ""
        h_km = max(0.0, alt * 0.3048 - self.home_elev_m) / 1000
        slant = math.hypot(dist_km, h_km)
        kind = aircraft_kind(p)
        big = 1.3 if kind == "jet" and (p.get("category") or "").upper() == "A5" else 1.0
        angle = math.degrees(math.atan2(h_km, max(dist_km, 0.05)))
        sun = getattr(self, "sun", None)
        cloud, vis_km = self.weather.cloud, self.weather.visibility_km
        eff = slant / (SIZE_FACTOR.get(kind, 1.0) * big)
        if angle < 2:
            seen = "Not visible (too low)"
        elif isinstance(vis_km, (int, float)) and slant > vis_km:
            seen = "Not visible (haze)"
        elif sun and sun[1] < -6:
            seen = "Lights only" if eff < 30 else "Not visible"
        elif isinstance(cloud, (int, float)) and cloud >= 90 and alt > 4000:
            seen = "Hard to see (overcast)"
        else:
            seen = "Easy to see" if eff < 4 else "Visible" if eff < 10 else "Hard to see" if eff < 22 else "Not visible"
        db = NOISE_AT_300M.get(kind, 92) + (3 if big > 1 else 0) - 20 * math.log10(max(slant * 1000, 100) / 300) \
            - 0.004 * max(0.0, slant * 1000 - 300)
        return seen, loudness_words(db)

    def _replay_controls(self, now):
        """Slider and buttons at the bottom while replaying."""
        rp = self.replay
        hist = self.feed.history if self.feed else []
        if not hist:
            self.replay = None
            return
        t0, t1 = hist[0][0], hist[-1][0]
        if rp["playing"]:
            rp["t"] += (now - rp["last"]) * 10              # 10x speed
            if rp["t"] >= t1:
                self.replay = None
                return
        rp["last"] = now
        self.replay_rects = []
        bar = pygame.Rect(C - 300, 790, 600, 14)
        pygame.draw.rect(self.canvas, PANEL_BG, bar.inflate(24, 70).move(0, 22), border_radius=16)
        pygame.draw.rect(self.canvas, RING, bar, border_radius=7)
        frac = (rp["t"] - t0) / max(1.0, t1 - t0)
        pygame.draw.circle(self.canvas, SELECT, (bar.x + frac * bar.width, bar.centery), 13)
        self.replay_rects.append((bar.inflate(0, 40), "seek"))
        x = C - (4 * 130 + 3 * 10) // 2
        for label, action in (("-1 min", "back"), ("Pause" if rp["playing"] else "Play", "play"), ("+1 min", "fwd"), ("Live", "live")):
            rect = pygame.Rect(x, 820, 130, 52)
            draw_button(self, rect, label, primary=(action == "live"), size=28)
            self.replay_rects.append((rect, action))
            x += 140
        ago = (time.time() - rp["t"]) / 60
        self.blit_text(f"REPLAY  {fmt_clock(rp['t'])}   ({ago:.0f} min ago)", 34, SELECT, (C, 160), bg=PANEL_BG)

    def _replay_tap(self, x, y):
        rp = self.replay
        hist = self.feed.history if self.feed else []
        if not rp or not hist:
            return False
        t0, t1 = hist[0][0], hist[-1][0]
        for rect, action in self.replay_rects:
            if rect.collidepoint(x, y):
                if action == "seek":
                    rp["t"] = t0 + (t1 - t0) * min(1.0, max(0.0, (x - (C - 300)) / 600))
                elif action == "back":
                    rp["t"] = max(t0, rp["t"] - 60)
                elif action == "fwd":
                    rp["t"] = min(t1, rp["t"] + 60)
                elif action == "play":
                    rp["playing"] = not rp["playing"]
                elif action == "live":
                    self.replay = None
                return True
        return False

    def _sample_traffic(self, aircraft, now):
        """Every 5 minutes: how many planes (and military) are within 50 km of you. Kept for 24 h."""
        if now - self._traffic_at < 300 or not aircraft:
            return
        self._traffic_at = now
        near = [p for p in aircraft.values() if p.get("_dist_km", 999) <= 50]
        self.traffic.append((now, len(near), sum(1 for p in near if is_military(p))))
        self.traffic = [t for t in self.traffic if now - t[0] <= 86400]
        try:
            with open(os.path.join(HERE, "traffic.json"), "w", encoding="utf-8") as f:
                json.dump(self.traffic, f)
        except OSError:
            pass

    def _user_took_over(self):
        if self.auto:
            self.auto["user"] = True

    def zoom(self, step):
        self._user_took_over()
        self.range_idx = max(0, min(len(self.ranges) - 1, self.range_idx + step))

    # -- view / projection ------------------------------------------------------
    def _update_view(self):
        self.cos_lat = math.cos(math.radians(self.view_lat))
        self._merc_v = math.asinh(math.tan(math.radians(self.view_lat)))

    def recenter(self):
        self.home_lat, self.home_lon = self.location.current()
        self.view_lat, self.view_lon = self.home_lat, self.home_lon
        self._update_view()

    def at_home(self):
        return km_between((self.view_lat, self.view_lon), (self.home_lat, self.home_lon)) < self.range_km * 0.02

    def offset_km(self, lat, lon):
        """km east/north of the screen centre."""
        return (lon - self.view_lon) * KM_PER_DEG * self.cos_lat, (lat - self.view_lat) * KM_PER_DEG

    def home_offset_km(self, lat, lon):
        """km east/north of your location."""
        return ((lon - self.home_lon) * KM_PER_DEG * math.cos(math.radians(self.home_lat)),
                (lat - self.home_lat) * KM_PER_DEG)

    def to_canvas(self, lat, lon):
        """Mercator, like the map tiles, scaled to be true at the screen centre."""
        k = EARTH_RADIUS_KM * self.cos_lat * R / self.range_km       # canvas px per radian
        x = C + math.radians(lon - self.view_lon) * k
        y = C - (math.asinh(math.tan(math.radians(lat))) - self._merc_v) * k
        return x, y

    def pan(self, dx, dy):
        """Move the map by (dx, dy) canvas pixels, like dragging in a map app."""
        k = EARTH_RADIUS_KM * self.cos_lat * R / self.range_km
        self.view_lon = (self.view_lon - math.degrees(dx / k) + 180) % 360 - 180
        merc = self._merc_v + dy / k
        self.view_lat = max(-80.0, min(80.0, math.degrees(math.atan(math.sinh(merc)))))
        self._update_view()

    def night_active(self, now):
        mode = self.settings.night_mode
        if mode not in ("dim", "blank"):
            return False
        def mins(hhmm):
            h, m = (int(v) for v in str(hhmm).split(":"))
            return h * 60 + m
        try:
            start, end = mins(self.cfg.get("night_start", "22:00")), mins(self.cfg.get("night_end", "07:00"))
        except ValueError:
            return False
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        return (start <= cur < end) if start < end else (cur >= start or cur < end)

    def asleep(self, now):
        """Night mode is hiding the radar right now (no recent touch, no military alert)."""
        return self.night_active(now) and now > self.wake_until and not self._mil_alert

    def press(self, x, y):
        now = time.time()
        if self.asleep(now):
            self.wake_until = now + 60        # first touch only wakes the screen
            self._swallow, self._press = True, None
            return
        self._user_took_over()
        self._press = self._last_drag = (x, y)
        self.dragging = False

    def drag(self, x, y):
        if self._press is None:
            return
        if not self.dragging and math.hypot(x - self._press[0], y - self._press[1]) > 12:
            self.dragging = True          # moved enough: this is a drag, not a tap
        if self.dragging:
            self.pan(x - self._last_drag[0], y - self._last_drag[1])
            self._last_drag = (x, y)

    # -- pinch to zoom -------------------------------------------------------------
    def from_canvas(self, x, y):
        k = EARTH_RADIUS_KM * self.cos_lat * R / self.range_km
        lon = self.view_lon + math.degrees((x - C) / k)
        lat = math.degrees(math.atan(math.sinh(self._merc_v - (y - C) / k)))
        return lat, lon

    def _place(self, geo, pos):
        """Move the view so that map point geo sits under screen position pos."""
        k = EARTH_RADIUS_KM * self.cos_lat * R / self.range_km
        self.view_lon = (geo[1] - math.degrees((pos[0] - C) / k) + 180) % 360 - 180
        merc = math.asinh(math.tan(math.radians(geo[0]))) + (pos[1] - C) / k
        self.view_lat = max(-80.0, min(80.0, math.degrees(math.atan(math.sinh(merc)))))
        self._update_view()

    def _two_fingers(self):
        (x1, y1), (x2, y2) = list(self.fingers.values())[:2]
        return max(20.0, math.hypot(x2 - x1, y2 - y1)), ((x1 + x2) / 2, (y1 + y2) / 2)

    def finger_down(self, fid, x, y):
        self._user_took_over()
        self.fingers[fid] = (x, y)
        if len(self.fingers) == 2 and self.pinch is None:
            dist, mid = self._two_fingers()
            self.pinch = {"dist": dist, "range": self.range_km, "geo": self.from_canvas(*mid)}
            self._press = None            # a second finger cancels the one-finger drag or tap
            self.dragging = False

    def finger_move(self, fid, x, y):
        if fid not in self.fingers:
            return
        self.fingers[fid] = (x, y)
        if self.pinch and len(self.fingers) >= 2:
            dist, mid = self._two_fingers()
            r = self.pinch["range"] * self.pinch["dist"] / dist      # fingers apart -> zoom in
            self.live_range = max(self.ranges[0], min(self.ranges[-1], r))
            self._place(self.pinch["geo"], mid)                      # pinched spot stays under the fingers

    def finger_up(self, fid):
        self.fingers.pop(fid, None)
        if self.pinch and len(self.fingers) < 2:
            r = self.live_range or self.range_km
            mid = self._two_fingers()[1] if len(self.fingers) >= 2 else (C, C)
            geo = self.from_canvas(*mid)
            # settle on the nearest zoom step
            self.range_idx = min(range(len(self.ranges)), key=lambda i: abs(math.log(self.ranges[i] / r)))
            self.live_range = None
            self.pinch = None
            self._place(geo, mid)

    def release(self, x, y):
        if self._swallow:
            self._swallow = False
            return
        if self._press is not None and not self.dragging:
            self.tap(x, y)
        self._press = None
        self.dragging = False

    def font(self, size):
        """Font for a design size, scaled up when Settings -> Text size is Large (radar view only)."""
        actual = int(round(size * self.text_scale))
        f = self.fonts.get(actual)
        if f is None:
            f = self.fonts[actual] = pygame.font.Font(None, actual)
        return f

    def text(self, s, size, color):
        key = (s, int(round(size * self.text_scale)), color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 600:
                self._text_cache.clear()
            surf = self._text_cache[key] = self.font(size).render(s, True, color)
        return surf

    def blit_text(self, s, size, color, pos, anchor="center", bg=None):
        surf = self.text(s, size, color)
        rect = surf.get_rect(**{anchor: pos})
        if bg:
            pygame.draw.rect(self.canvas, bg, rect.inflate(20, 8), border_radius=10)
        self.canvas.blit(surf, rect)

    # -- input --------------------------------------------------------------
    def tap(self, x, y):
        if self.settings.active:
            self.settings.tap(x, y)
            return
        if self.replay and self._replay_tap(x, y):
            return
        if math.hypot(x - self.btn_home[0], y - self.btn_home[1]) < 45:
            self.recenter()
            return
        if math.hypot(x - self.btn_settings[0], y - self.btn_settings[1]) < 45 or \
                (abs(x - C) < 200 and abs(y - 100) < 25):      # gear button or the place name
            self.settings.open_menu()
            return
        for (bx, by), step in ((self.btn_minus, 1), (self.btn_plus, -1)):
            if math.hypot(x - bx, y - by) < 50:
                self.zoom(step)
                return
        if self.map and math.hypot(x - self.btn_map[0], y - self.btn_map[1]) < 50:
            self.show_map = not self.show_map
            return
        if self.alert_rect and self.alert_rect.collidepoint(x, y):
            self.selected, self.card_collapsed = self.alert_hex, False   # tap the alert: show that plane
            return
        if self.selected and self.card_rect and self.card_rect.collidepoint(x, y):
            self.card_collapsed = not self.card_collapsed   # tap the card: shrink it to see the path
            return
        best, best_d = None, 45
        for hx, (px, py) in self.screen_pos.items():
            d = math.hypot(x - px, y - py)
            if d < best_d:
                best, best_d = hx, d
        if best is None:                       # empty map: a quick second tap zooms in on that spot
            t0, x0, y0 = self._last_empty_tap
            now = time.time()
            if now - t0 < 0.4 and math.hypot(x - x0, y - y0) < 60:
                geo = self.from_canvas(x, y)
                self.zoom(-1)
                self._place(geo, (x, y))
                self._last_empty_tap = (0.0, 0.0, 0.0)
                return
            self._last_empty_tap = (now, x, y)
        if best != self.selected:
            self.card_collapsed = False
        self.selected = best

    # -- drawing ------------------------------------------------------------
    def draw(self, feed, now):
        cv = self.canvas
        self.feed = feed                       # for the airport board and feed status screens
        self.settings.update(now)
        if self.settings.active:
            self.text_scale = 1.0              # settings screens keep their normal size
            self.settings.draw(now)
            return cv
        self.text_scale = 1.25 if self.settings.text_large else 1.0
        aircraft, mil_no_pos, trails, status, last_ok = feed.snapshot()
        replaying = self.replay is not None
        if replaying:
            aircraft, trails = feed.history_at(self.replay["t"])
            mil_no_pos, status = [], "Replay - alerts paused"
        self.home_lat, self.home_lon = self.location.current()
        self._sync_feed(feed, now)
        cv.fill(BLACK)
        pygame.draw.circle(cv, SCOPE_BG, (C, C), C)
        base = None
        if self.show_map:
            if self.live_range is None:
                self.map.get(self.range_km, (self.view_lat, self.view_lon))   # build this zoom if needed
            base = self.map.nearest(self.range_km)
            if base:
                self._blit_map(*base)

        self._draw_sweep(now)
        self._draw_grid()

        # aircraft
        self.screen_pos = {}
        in_range, mil_count = [], 0
        for hx, p in aircraft.items():
            hx_, hy_ = self.home_offset_km(p["lat"], p["lon"])
            p["_dist_km"] = math.hypot(hx_, hy_)            # distance from you (the blue dot)
            if is_military(p) and p["_dist_km"] <= self.sightings.radius and not replaying:
                self.sightings.record(p, self.info.get(p)["model"], compass_direction(hx_, hy_), now)
            alt = p.get("alt_baro")
            if self.settings.max_alt and isinstance(alt, (int, float)) and alt > self.settings.max_alt:
                continue                                    # height filter: hidden from the map and alerts
            dx, dy = self.offset_km(p["lat"], p["lon"])
            if math.hypot(dx, dy) > self.range_km:          # outside the circle on screen
                continue
            in_range.append(hx)
            if is_military(p):
                mil_count += 1
        if self.selected and self.selected not in aircraft:
            self.selected = None

        for hx in in_range:  # trails underneath the icons
            if hx == self.selected:
                continue                  # the selected plane gets its full path instead
            pts = [self.to_canvas(la, lo) for la, lo in trails.get(hx, [])]
            if len(pts) > 1:
                col = MIL_DIM if is_military(aircraft[hx]) else CIVIL_DIM
                pygame.draw.lines(cv, col, False, pts, 2)

        if self.selected:
            self._draw_selected_path(aircraft[self.selected], trails.get(self.selected, []))

        # draw military last so they sit on top
        for hx in sorted(in_range, key=lambda h: is_military(aircraft[h])):
            self._draw_plane(hx, aircraft[hx])

        self._draw_home()
        self._draw_sun(now)
        cv.blit(self._outside, (0, 0))    # trim map, paths and labels to the round screen

        self.sightings.maybe_save(now)
        if not replaying:
            self._sample_traffic(aircraft, now)
        self.alert_rect, self.alert_hex = None, None
        shown = aircraft if not self.settings.max_alt else {
            h: p for h, p in aircraft.items()
            if not (isinstance(p.get("alt_baro"), (int, float)) and p["alt_baro"] > self.settings.max_alt)}
        alert = None if replaying else self._find_alert(shown)
        self._mil_alert = bool(alert and alert[0] == 2)
        self._auto_zoom(alert, now)
        if alert:
            self._draw_alert(*alert, now)

        # HUD
        hud_bg = PANEL_BG
        place = self.location.name
        clouds = self.weather.label()
        if self.weather.contrails:
            self.blit_text(f"Contrails: {self.weather.contrails}", 24, TEXT_DIM, (C, 136), bg=hud_bg)
        self.blit_text(f"{place}  {fmt_clock()}" + (f"  |  {clouds}" if clouds else ""), 34, TEXT, (C, 100), bg=hud_bg)
        summary = f"{len(in_range)} aircraft  |  {mil_count} military  |  {fmt_dist(self.range_km)}" + (
            f"  |  < {fmt_alt_ft(self.settings.max_alt)}" if self.settings.max_alt else "")
        self.blit_text(summary, 34, TEXT, (C, 930), bg=hud_bg)
        if mil_no_pos:
            self.blit_text(f"+{len(mil_no_pos)} military heard, no position", 28, MIL, (C, 893), bg=hud_bg)
        if self.health.hot():
            self.blit_text(f"Pi is hot ({self.health.temp:.0f}\u00b0C) - check the cooler" if self.health.temp else
                           "Pi is slowing down - check power and cooling", 28, WARN, (C, 856), bg=hud_bg)
        stale = last_ok and now - last_ok > 30
        self.blit_text(status + ("  (stale)" if stale else ""), 24, WARN if (stale or "error" in status) else TEXT_DIM, (C, 966), bg=hud_bg)
        if base:
            self.blit_text("Map (c) OpenStreetMap contributors", 24, TEXT_DIM, (C, 1000))
        elif self.show_map:
            self.blit_text("Loading map...", 24, TEXT_DIM, (C, 1000))

        self._draw_buttons()
        if replaying:
            self._replay_controls(now)
        self.card_rect = None
        if self.selected:
            scale, self.text_scale = self.text_scale, 1.0      # the details card has a fixed layout
            self._draw_info(aircraft[self.selected], now)
            self.text_scale = scale

        if self.asleep(now):                  # night mode
            if self.settings.night_mode == "dim":
                self._night_fill.set_alpha(175)
                cv.blit(self._night_fill, (0, 0))
            else:
                cv.fill(BLACK)
                self.blit_text(fmt_clock(), 34, (20, 60, 50), (C, C))
        return cv

    def _draw_sweep(self, now):
        angle = (now * 90) % 360          # one turn every 4 s
        trail_deg, slices = 50, 25
        ss = self.sweep_surf
        ss.fill((0, 0, 0, 0))
        for i in range(slices - 1, -1, -1):
            a0 = math.radians(angle - trail_deg * (i + 1) / slices)
            a1 = math.radians(angle - trail_deg * i / slices)
            alpha = int(255 * 0.35 * (1 - i / slices))   # translucent so the map shows through
            pygame.draw.polygon(ss, (*SWEEP, alpha), [(C, C),
                                                      (C + C * math.sin(a0), C - C * math.cos(a0)),
                                                      (C + C * math.sin(a1), C - C * math.cos(a1))])
        self.canvas.blit(ss, (0, 0))

    def _draw_grid(self):
        cv = self.canvas
        pygame.draw.line(cv, GRID, (C, C - R), (C, C + R))
        pygame.draw.line(cv, GRID, (C - R, C), (C + R, C))
        for frac in (0.25, 0.5, 0.75, 1.0):
            pygame.draw.circle(cv, RING, (C, C), int(R * frac), 2 if frac == 1.0 else 1)
            label = fmt_dist(round(self.range_km * frac, 4))
            a = math.radians(45)
            self.blit_text(label, 24, TEXT_DIM, (C + R * frac * math.sin(a) + 6, C - R * frac * math.cos(a) - 6), "bottomleft")
        for label, a in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            r = R + 22
            self.blit_text(label, 34, SWEEP, (C + r * math.sin(math.radians(a)), C - r * math.cos(math.radians(a))))

    def _draw_plane(self, hx, p):
        x, y = self.to_canvas(p["lat"], p["lon"])
        self.screen_pos[hx] = (x, y)
        mil = is_military(p)
        col = SELECT if hx == self.selected else (MIL if mil else CIVIL)
        t = math.radians(p.get("track", p.get("true_heading", 0)) or 0)
        ct, st = math.cos(t), math.sin(t)
        on_ground = p.get("alt_baro") == "ground"
        kind = aircraft_kind(p)
        scale = 0.6 if on_ground else (1.35 if mil else 1.0)
        if kind == "jet" and (p.get("category") or "").upper() == "A5":
            scale *= 1.2                                     # wide-body twins a bit bigger
        shape = {"fighter": FIGHTER_SHAPE, "four": HEAVY_SHAPE, "light": LIGHT_SHAPE, "heli": HELI_SHAPE}.get(kind, PLANE_SHAPE)

        def turn(px, py):
            return x + (px * ct - py * st) * scale, y + (px * st + py * ct) * scale
        pygame.draw.polygon(self.canvas, col, [turn(px, py) for px, py in shape])
        if kind == "four":
            for ex, ey in HEAVY_ENGINES:
                pygame.draw.circle(self.canvas, col, turn(ex, ey + 3), max(2, int(2.5 * scale)))
        elif kind == "heli":
            spin = time.time() * 9                           # turning rotor
            for k in range(2):
                a = spin + k * math.pi / 2
                pygame.draw.line(self.canvas, col, turn(13 * math.cos(a), 13 * math.sin(a) - 1),
                                 turn(-13 * math.cos(a), -13 * math.sin(a) - 1), 2)
        if on_ground and hx != self.selected:
            return            # parked/taxiing aircraft: icon only, no clutter
        if x > C + 250:   # near the right edge of the round screen: label on the left
            self.blit_text(callsign(p), 28, col, (x - 20, y - 18), "topright")
            self.blit_text(fmt_alt(p), 24, col, (x - 20, y + 2), "topright")
        else:
            self.blit_text(callsign(p), 28, col, (x + 20, y - 18), "topleft")
            self.blit_text(fmt_alt(p), 24, col, (x + 20, y + 2), "topleft")

    def _find_alert(self, aircraft):
        """Most urgent plane for the alert banner, or None. Returns (level, hex, plane) with plane["_alert"] set.

        A plane counts when it is inside your alert zone (within military_alert_km / civil_alert_km and in a
        direction chosen in Settings -> Alert directions), or when its current speed and heading will bring it
        into the zone within warning_lead_seconds ("incoming"). Military beats everything else, then planes
        already in the zone, then whichever arrives first. Planes on the ground never alert."""
        mil_km = self.cfg.get("military_alert_km", 5)
        civ_km = self.cfg.get("civil_alert_km", 5)
        lead = self.cfg.get("warning_lead_seconds", 180)
        dirs = self.settings.alert_dirs
        best, best_rank = None, None
        for hx, p in aircraft.items():
            if p.get("alt_baro") == "ground":
                continue
            mil = is_military(p)
            radius = mil_km if mil else civ_km
            e, n = self.home_offset_km(p["lat"], p["lon"])
            d = math.hypot(e, n)
            p["_dir"] = compass_direction(e, n)
            alert = None
            squawk = str(p.get("squawk") or "")
            if squawk in EMERGENCY_SQUAWKS:
                # emergencies show at any distance and in any direction, ranked below military jets in your zone
                alert = {"kind": "emergency", "dir": p["_dir"], "eta": 0, "dist": d, "squawk": squawk}
                rank = (2, True, 0, -d)
                if best_rank is None or rank > best_rank:
                    p["_alert"] = alert
                    best, best_rank = (3, hx, p), rank
                continue
            if d <= radius and p["_dir"] in dirs:
                alert = {"kind": "now", "dir": p["_dir"], "eta": 0, "dist": d}
            else:
                gs = p.get("gs")
                if isinstance(gs, (int, float)) and gs >= 40 and lead > 0:
                    v = gs * 1.852 / 3600                       # km per second
                    if d - v * lead <= radius:                  # close enough to get there in time
                        t_rad = math.radians(p.get("track") or 0)
                        ve, vn = v * math.sin(t_rad), v * math.cos(t_rad)
                        for t in range(2, int(lead) + 1, 2):    # walk along the path, 2 s at a time
                            pe, pn = e + ve * t, n + vn * t
                            if math.hypot(pe, pn) <= radius and compass_direction(pe, pn) in dirs:
                                alert = {"kind": "soon", "dir": compass_direction(pe, pn), "eta": t,
                                         "dist": math.hypot(pe, pn), "e": pe, "n": pn}
                                break
            if alert is None:
                continue
            rank = (3 if mil else 1, alert["kind"] == "now", -alert["eta"], -d)
            if best_rank is None or rank > best_rank:
                p["_alert"] = alert
                best, best_rank = (2 if mil else 1, hx, p), rank
        return best

    # -- how high to look ------------------------------------------------------------
    def _start_elevation_lookup(self):
        """Ground height of your home (m above sea level): config override, cached value, or Open-Meteo."""
        override = self.cfg.get("home_elevation_m")
        if isinstance(override, (int, float)):
            self.home_elev_m = float(override)
            return
        lat, lon = self.location.current()
        key = f"{lat:.3f},{lon:.3f}"                     # about 100 m - close enough for ground height
        cached = load_prefs().get("elevation", {})
        if isinstance(cached, dict) and key in cached:
            self.home_elev_m = float(cached[key])
            return
        self.home_elev_m = 0.0

        def work():
            try:
                url = f"{ELEVATION_URL}?latitude={lat:.3f}&longitude={lon:.3f}"
                req = urllib.request.Request(url, headers={"User-Agent": "sky-radar/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    height = float(json.load(resp)["elevation"][0])
                self.home_elev_m = height
                save_prefs(elevation={key: height})
            except Exception:
                pass                                     # offline: angles use sea level until next start
        threading.Thread(target=work, daemon=True).start()

    def _alert_point(self, p):
        """(lat, lon) where you'll see the alert plane: now, or where it enters your zone."""
        al = p.get("_alert") or {}
        if al.get("kind") == "soon" and "e" in al:
            return (self.home_lat + al["n"] / KM_PER_DEG,
                    self.home_lon + al["e"] / (KM_PER_DEG * math.cos(math.radians(self.home_lat))))
        return p["lat"], p["lon"]

    def _auto_zoom(self, alert, now):
        """While a (non-emergency) alert is on, keep you and the plane on screen; afterwards go back."""
        if not self.cfg.get("auto_zoom", True):
            return
        if alert and alert[0] in (1, 2):
            _, hx, p = alert
            if self.auto is None:
                self.auto = {"saved": (self.view_lat, self.view_lon, self.range_idx), "user": False, "hex": None}
            self.auto["seen"] = now
            if self.auto["user"]:
                return
            pts = [(self.home_lat, self.home_lon), (p["lat"], p["lon"]), self._alert_point(p)]
            visible = all(km_between((self.view_lat, self.view_lon), q) <= 0.85 * self.range_km for q in pts)
            if self.auto["hex"] == hx and visible:
                return
            self.auto["hex"] = hx
            lats, lons = [q[0] for q in pts], [q[1] for q in pts]
            center = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
            need = max(km_between(center, q) for q in pts) * 1.35 + 0.3
            self.range_idx = next((i for i, r in enumerate(self.ranges) if r >= need), len(self.ranges) - 1)
            self.live_range = None
            self.view_lat, self.view_lon = center
            self._update_view()
        elif self.auto and now - self.auto.get("seen", 0) > 8:
            if not self.auto["user"]:
                self.view_lat, self.view_lon, self.range_idx = self.auto["saved"]
                self._update_view()
            self.auto = None

    def _draw_sun(self, now):
        """Small sun on the radar ring, in the sun's compass direction (only while it is up)."""
        az, el = sun_position(self.home_lat, self.home_lon, now)
        self.sun = (az, el)
        if el < -2:
            return
        r = R + 20                      # in the band outside the radar ring, clear of the text
        x, y = C + r * math.sin(math.radians(az)), C - r * math.cos(math.radians(az))
        col = (250, 200, 60)
        for k in range(8):
            a = math.radians(k * 45)
            pygame.draw.line(self.canvas, col, (x + 9 * math.cos(a), y + 9 * math.sin(a)),
                             (x + 14 * math.cos(a), y + 14 * math.sin(a)), 2)
        pygame.draw.circle(self.canvas, col, (x, y), 7)

    def _sun_glare(self, p, dist_km):
        """Hint when the plane will be close to the sun as seen from home."""
        sun = getattr(self, "sun", None)
        angle = self._look_angle(p, dist_km)
        if not sun or sun[1] <= 0 or angle is None:
            return ""
        lat, lon = self._alert_point(p)
        e, n = self.home_offset_km(lat, lon)
        plane_az = math.degrees(math.atan2(e, n)) % 360
        return "Near the sun - shade your eyes" if angle_between(plane_az, angle, sun[0], sun[1]) < 20 else ""

    def _look_angle(self, p, dist_km):
        """Angle above the horizon in degrees, or None if the altitude is unknown."""
        alt = p.get("alt_geom") if isinstance(p.get("alt_geom"), (int, float)) else p.get("alt_baro")
        if not isinstance(alt, (int, float)):
            return None
        return math.degrees(math.atan2(alt * 0.3048 - self.home_elev_m, max(dist_km, 0.05) * 1000))

    def _look_up(self, p, dist_km):
        """Angle above the horizon to look at this plane, as text (e.g. "25\u00b0 UP")."""
        angle = self._look_angle(p, dist_km)
        if angle is None:
            return ""
        return "LOW ON THE HORIZON" if angle < 4 else f"{round(angle)}\u00b0 UP"

    def _draw_alert(self, level, hx, p, now):
        """Blinking red (military) or yellow (other aircraft) screen, edge ring and a banner."""
        cv = self.canvas
        emergency = level == 3
        red = level >= 2
        col = ALERT_RED if red else ALERT_YELLOW
        alert = p.get("_alert") or {"kind": "now", "dir": p.get("_dir"), "eta": 0, "dist": p["_dist_km"]}
        soon = alert["kind"] == "soon"
        pulse = 0.5 + 0.5 * math.sin(now * (7 if red else 5))
        strength = 0.45 if soon else 1.0                 # incoming: same colours, calmer blink
        fill = self._alert_fill["red" if red else "yellow"]
        fill.set_alpha(int(((40 + 80 * pulse) if red else (25 + 55 * pulse)) * strength))
        cv.blit(fill, (0, 0))
        pygame.draw.circle(cv, col, (C, C), C - 14, int((12 + 12 * pulse) * (0.5 if soon else 1)))
        cv.blit(self._outside, (0, 0))

        info = self.info.get(p)
        d = p["_dist_km"]
        where = DIRECTION_NAMES.get(alert["dir"], "")
        up = self._look_up(p, alert["dist"])
        if emergency:
            title = f"EMERGENCY {alert['squawk']}: {EMERGENCY_SQUAWKS[alert['squawk']]}"
            parts = [callsign(p), info["model"] or p.get("t") or "",
                     f"{fmt_dist(round(d, 1 if d >= 1 else 2))} {alert['dir']}", fmt_alt(p)]
            px, py = self.to_canvas(p["lat"], p["lon"])
            if math.hypot(px - C, py - C) < R:
                pygame.draw.circle(cv, ALERT_RED, (px, py), int(26 + 14 * pulse), 4)   # ring around the plane
        elif soon:
            eta = int(alert["eta"])
            title = f"{'MILITARY' if red else 'AIRCRAFT'} INCOMING - {where} IN {eta // 60}:{eta % 60:02d}"
            parts = [callsign(p), info["model"] or p.get("t") or "", f"{fmt_dist(round(d, 1 if d >= 1 else 2))} away",
                     f"then look {up.lower()}" if up else ""]
        else:
            title = f"{'MILITARY ALERT' if red else 'AIRCRAFT NEARBY'} - LOOK {where}" + (f", {up}" if up else "")
            parts = [callsign(p), info["model"] or p.get("t") or "", f"{fmt_dist(round(d, 1 if d >= 1 else 2))} away", fmt_alt(p)]
        detail = "  |  ".join(x for x in parts if x)
        seen, loud = self.spotting(p, alert["dist"]) if not emergency else ("", "")
        rating = ", ".join(x for x in (seen.lower(), loud) if x)
        hint = "" if emergency else "  |  ".join(h for h in (rating, self._sun_glare(p, alert["dist"]), self.weather.spotting_hint()) if h)
        tsize = 44 if self.font(44).size(title)[0] <= 780 else 34
        w = min(1000, max(self.font(tsize).size(title)[0], self.font(28).size(detail)[0], self.font(24).size(hint)[0]) + 48)
        rect = pygame.Rect(0, 0, w, 128 if hint else 100)
        rect.midtop = (C, 156)
        pygame.draw.rect(cv, (12, 6, 6) if red else (14, 12, 4), rect, border_radius=16)
        pygame.draw.rect(cv, col, rect, 4 if not soon else 2, border_radius=16)
        self.blit_text(title, tsize, col, (C, rect.y + 32))
        self.blit_text(detail, 28, (240, 245, 245), (C, rect.y + 72))
        if hint:
            self.blit_text(hint, 24, ALERT_YELLOW, (C, rect.y + 104))
        self.alert_rect, self.alert_hex = rect, hx

    def _draw_buttons(self):
        for (bx, by), sign in ((self.btn_minus, "-"), (self.btn_plus, "+")):
            pygame.draw.circle(self.canvas, SCOPE_BG, (bx, by), 38)
            pygame.draw.circle(self.canvas, RING, (bx, by), 38, 2)
            self.blit_text(sign, 44, SWEEP, (bx, by + 2))
        if self.map:
            bx, by = self.btn_map
            pygame.draw.circle(self.canvas, SCOPE_BG, (bx, by), 38)
            pygame.draw.circle(self.canvas, SWEEP if self.show_map else RING, (bx, by), 38, 2)
            self.blit_text("MAP", 28, SWEEP if self.show_map else TEXT_DIM, (bx, by + 1))
        self._draw_gear(*self.btn_settings)
        self._draw_locate(*self.btn_home)

    def _blit_map(self, surf, center, built_range):
        """Draw a pre-rendered map, scaled when it was built for a different zoom (pinching)."""
        mx, my = self.to_canvas(*center)
        size = surf.get_width()
        f = built_range / self.range_km                      # screen px per map px
        if abs(f - 1) < 1e-3:
            self.canvas.blit(surf, (mx - size / 2, my - size / 2))
            return
        left, top = mx - size * f / 2, my - size * f / 2
        src = pygame.Rect(int(-left / f), int(-top / f), int(CANVAS / f) + 2, int(CANVAS / f) + 2).clip(surf.get_rect())
        if src.width < 2 or src.height < 2:
            return
        part = pygame.transform.scale(surf.subsurface(src), (max(1, int(src.width * f)), max(1, int(src.height * f))))
        self.canvas.blit(part, (left + src.x * f, top + src.y * f))

    def _draw_locate(self, bx, by):
        """'Back to my location' button: crosshair, blue when you have moved away."""
        col = SWEEP if self.at_home() else HOME_BLUE
        pygame.draw.circle(self.canvas, SCOPE_BG, (bx, by), 32)
        pygame.draw.circle(self.canvas, RING if self.at_home() else HOME_BLUE, (bx, by), 32, 2)
        pygame.draw.circle(self.canvas, col, (bx, by), 11, 3)
        pygame.draw.circle(self.canvas, col, (bx, by), 4)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            pygame.draw.line(self.canvas, col, (bx + dx * 11, by + dy * 11), (bx + dx * 19, by + dy * 19), 3)

    def _draw_home(self):
        """Your location: blue dot with a white ring, or an arrow at the edge when it is off screen."""
        x, y = self.to_canvas(self.home_lat, self.home_lon)
        d = math.hypot(x - C, y - C)
        if d < R - 12:
            self.canvas.blit(self._halo, (x - 32, y - 32))
            pygame.draw.circle(self.canvas, (255, 255, 255), (x, y), 11)
            pygame.draw.circle(self.canvas, HOME_BLUE, (x, y), 8)
            return
        a = math.atan2(x - C, -(y - C))   # direction from the screen centre to you
        tip = (C + (R - 6) * math.sin(a), C - (R - 6) * math.cos(a))
        base_r = R - 30
        left = (C + base_r * math.sin(a - 0.05), C - base_r * math.cos(a - 0.05))
        right = (C + base_r * math.sin(a + 0.05), C - base_r * math.cos(a + 0.05))
        pygame.draw.polygon(self.canvas, (255, 255, 255), [tip, left, right])
        pygame.draw.polygon(self.canvas, HOME_BLUE, [tip, left, right], 0)
        pygame.draw.polygon(self.canvas, (255, 255, 255), [tip, left, right], 2)

    def _sync_feed(self, feed, now):
        """Fetch planes around what is on screen; refresh soon after the view moves somewhere new."""
        radius = min(250, max(self.cfg["fetch_radius_nm"], self.range_km * 1.5 / 1.852))
        view = (round(self.view_lat, 1), round(self.view_lon, 1), int(radius))
        feed.view = lambda: view
        if view != self._feed_key and not self.dragging and now - self._feed_wake > 2:
            if self._feed_key is not None:
                feed.wake.set()
            self._feed_key, self._feed_wake = view, now

    def _draw_gear(self, bx, by):
        """Small settings button with a drawn gear icon."""
        pygame.draw.circle(self.canvas, SCOPE_BG, (bx, by), 32)
        pygame.draw.circle(self.canvas, RING, (bx, by), 32, 2)
        teeth = []
        for i in range(16):                       # 8 teeth: alternate outer/inner radius
            a = math.radians(i * 22.5 + 11.25)
            r = 17 if i % 2 == 0 else 12
            for da in (-9, 9):
                teeth.append((bx + r * math.sin(a + math.radians(da)), by - r * math.cos(a + math.radians(da))))
        pygame.draw.polygon(self.canvas, SWEEP, teeth)
        pygame.draw.circle(self.canvas, SCOPE_BG, (bx, by), 6)

    def _polyline(self, latlons, color, width, dash=0):
        """Draw a path given in lat/lon, clipped to the screen; dash > 0 draws it dashed."""
        bounds = pygame.Rect(-50, -50, CANVAS + 100, CANVAS + 100)
        pts = [self.to_canvas(la, lo) for la, lo in latlons]
        carry = 0.0                       # keeps the dash pattern continuous across segments
        for a, b in zip(pts, pts[1:]):
            seg = bounds.clipline(a, b)
            if not seg:
                continue
            (x1, y1), (x2, y2) = seg
            if not dash:
                pygame.draw.line(self.canvas, color, (x1, y1), (x2, y2), width)
                continue
            length = math.hypot(x2 - x1, y2 - y1)
            pos = 0.0
            while pos < length:
                on = int((pos + carry) // dash) % 2 == 0
                step = min(dash - (pos + carry) % dash, length - pos)
                if on:
                    f0, f1 = pos / length, (pos + step) / length
                    pygame.draw.line(self.canvas, color, (x1 + (x2 - x1) * f0, y1 + (y2 - y1) * f0),
                                     (x1 + (x2 - x1) * f1, y1 + (y2 - y1) * f1), width)
                pos += step
            carry = (carry + length) % (2 * dash)

    def _draw_selected_path(self, p, live_trail):
        """Blue: where the plane has flown. White dashed: the way to its destination (civil only)."""
        flown = self.info.track(p) or live_trail
        here = (p["lat"], p["lon"])
        if flown:
            self._polyline(list(flown) + [here], PATH_BLUE, 4)
        if is_military(p):
            return                        # military flights don't share where they are going
        dest = self.info.get(p).get("dest")
        if dest and dest.get("lat") is not None:
            self._polyline(great_circle_points(here[0], here[1], dest["lat"], dest["lon"]), ROUTE_WHITE, 2, dash=14)
            dx, dy = self.to_canvas(dest["lat"], dest["lon"])
            if math.hypot(dx - C, dy - C) < R:
                pygame.draw.circle(self.canvas, ROUTE_WHITE, (dx, dy), 7, 2)
                self.blit_text(dest["code"], 28, ROUTE_WHITE, (dx + 12, dy - 10), "topleft")

    def _eta(self, p, info, now):
        """(label, value, detail) for the ETA tile."""
        gs = p.get("gs")
        if not isinstance(gs, (int, float)) or gs < 40 or p.get("alt_baro") == "ground":
            return ("ETA", "-", "on the ground")
        speed_kmh = gs * 1.852
        dest = info.get("dest")
        if not is_military(p) and dest and dest.get("lat") is not None:
            hours = great_circle_km(p["lat"], p["lon"], dest["lat"], dest["lon"]) / speed_kmh
            arrive = fmt_clock(now + hours * 3600)
            return (f"Arrives {dest['code']}", arrive, "in " + fmt_duration(hours))
        # no destination (military): when is it closest to you?
        dx, dy = self.home_offset_km(p["lat"], p["lon"])
        t = math.radians(p.get("track") or 0)
        vx, vy = speed_kmh * math.sin(t), speed_kmh * math.cos(t)
        t_cpa = -(dx * vx + dy * vy) / (speed_kmh ** 2)
        if t_cpa <= 0:
            return ("Closest to you", "passed", "moving away")
        miss = math.hypot(dx + vx * t_cpa, dy + vy * t_cpa)
        when = fmt_duration(t_cpa)
        return ("Closest to you", when if when == "now" else "in " + when, f"{fmt_dist(round(miss, 1 if miss >= 1 else 2))} away")

    def _draw_info(self, p, now):
        """Compact details card. It sits on the half of the screen away from the plane, so the plane stays visible."""
        cv = self.canvas
        mil = is_military(p)
        info = self.info.get(p)
        col = MIL if mil else SELECT
        px, py = self.to_canvas(p["lat"], p["lon"])
        at_top = py > C                   # plane in the lower half -> card goes to the top
        gs = p.get("gs")
        speed = f"{gs:.0f} kt" if isinstance(gs, (int, float)) else "-"
        alt = fmt_alt(p)

        if self.card_collapsed:
            parts = [callsign(p), info["model"] or "", alt, speed]
            label = "  |  ".join(x for x in parts if x)
            top_y = self.alert_rect.bottom + 30 if self.alert_rect else 180      # stay clear of an alert banner
            rect = self.text(label, 24, col).get_rect(center=(C, top_y if at_top else 845)).inflate(28, 18)
            self.card_rect = rect
            pygame.draw.rect(cv, PANEL_BG, rect, border_radius=12)
            pygame.draw.rect(cv, col, rect, 2, border_radius=12)
            self.blit_text(label, 24, col, rect.center)
            return

        loading = info["state"] == "loading"
        rect = pygame.Rect(0, 0, 480, 166 if mil else 222)
        if at_top:
            rect.midtop = (C, self.alert_rect.bottom + 12 if self.alert_rect else 158)   # below an alert banner
        else:
            rect.midbottom = (C, 878)
        self.card_rect = rect
        pygame.draw.rect(cv, PANEL_BG, rect, border_radius=14)
        pygame.draw.rect(cv, col, rect, 2, border_radius=14)
        x0, x1, y = rect.x + 18, rect.right - 18, rect.y + 12

        # model + military badge
        model = info["model"] or ("Looking up..." if loading else ("Type not broadcast" if mil else "Unknown type"))
        self.blit_text(model[:26], 34, col, (x0, y), "topleft")
        if mil:
            badge = self.text("MILITARY", 20, PANEL_BG).get_rect(topright=(x1, y + 4))
            pygame.draw.rect(cv, MIL, badge.inflate(12, 6), border_radius=6)
            cv.blit(self.text("MILITARY", 20, PANEL_BG), badge)
        y += 32

        # operator / airline + callsign
        who = info["operator"] or ("" if loading else ("Unknown operator" if mil else "Unknown airline"))
        self.blit_text("  |  ".join(x for x in (who[:30], callsign(p)) if x), 24, TEXT, (x0, y), "topleft")
        y += 28
        seen, loud = self.spotting(p, p.get("_dist_km", 0))
        if seen:
            self.blit_text(f"{seen}  |  {loud}", 24, SELECT, (x0, y), "topleft")
            y += 26

        if not mil:                       # route: FROM ---> TO
            o, d = info.get("origin"), info.get("dest")
            if o and d:
                self.blit_text(o["code"], 28, TEXT, (x0, y), "topleft")
                self.blit_text(d["code"], 28, TEXT, (x1, y), "topright")
                ax0, ax1, ay = x0 + 70, x1 - 80, y + 10
                pygame.draw.line(cv, col, (ax0, ay), (ax1, ay), 2)
                pygame.draw.polygon(cv, col, [(ax1 + 10, ay), (ax1, ay - 6), (ax1, ay + 6)])
                self.blit_text(o["city"][:22], 20, TEXT_DIM, (x0, y + 26), "topleft")
                self.blit_text(d["city"][:22], 20, TEXT_DIM, (x1, y + 26), "topright")
            else:
                self.blit_text("Looking up route..." if loading else "Route not known", 22, TEXT_DIM, (x0, y + 8), "topleft")
            y += 52

        # altitude, speed, ETA in three small columns
        pygame.draw.line(cv, GRID, (x0, y + 2), (x1, y + 2), 1)
        cols = [("Altitude", "on ground" if alt == "ground" else alt, ""),
                ("Speed", speed, ""),
                self._eta(p, info, now)]
        cw = (x1 - x0) / 3
        for i, (label, value, detail) in enumerate(cols):
            cx = x0 + cw * i + cw / 2
            self.blit_text(label, 20, TEXT_DIM, (cx, y + 16))
            self.blit_text(value, 28, TEXT, (cx, y + 38))
            if detail:
                self.blit_text(detail, 20, TEXT_DIM, (cx, y + 58))


# --------------------------------------------------------------------------- main loop
def make_display(cfg, fullscreen):
    if fullscreen:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    else:
        size = cfg.get("window_size", 800)
        screen = pygame.display.set_mode((size, size))
        pygame.mouse.set_visible(True)
    return screen


def main():
    cfg = load_config()
    if "--fullscreen" in sys.argv:
        cfg["fullscreen"] = True
    pygame.init()
    pygame.display.set_caption("Sky Radar")
    fullscreen = cfg.get("fullscreen", False)
    screen = make_display(cfg, fullscreen)
    clock = pygame.time.Clock()
    location = Location(cfg)
    feed = Feed(cfg, location)
    radar = Radar(cfg, location)
    last_beat = 0.0

    while True:
        sw, sh = screen.get_size()
        side = min(sw, sh)
        scale = side / CANVAS
        ox, oy = (sw - side) // 2, (sh - side) // 2
        rot = radar.settings.app_rotation

        def to_canvas(px, py):
            """Window position -> canvas position, undoing the app's own rotation (PC only)."""
            x, y = (px - ox) / scale, (py - oy) / scale
            if rot:
                a = math.radians(-rot)
                dx, dy = x - C, y - C
                x, y = C + dx * math.cos(a) - dy * math.sin(a), C + dx * math.sin(a) + dy * math.cos(a)
            return x, y

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.FINGERUP:            # always, so no finger is left "stuck down"
                radar.finger_up(ev.finger_id)
            elif ev.type in (pygame.FINGERDOWN, pygame.FINGERMOTION) and not radar.settings.active:
                fx, fy = to_canvas(ev.x * sw, ev.y * sh)   # touch is 0..1 of the window
                if ev.type == pygame.FINGERDOWN:
                    radar.finger_down(ev.finger_id, fx, fy)
                else:
                    radar.finger_move(ev.finger_id, fx, fy)
            if radar.settings.active:             # settings open: keys go to its text boxes
                if ev.type == pygame.TEXTINPUT:
                    radar.settings.type_text(ev.text)
                elif ev.type == pygame.KEYDOWN:
                    radar.settings.key(ev.key)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    radar.tap(*to_canvas(*ev.pos))
                continue
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    return
                if ev.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                    radar.zoom(-1)
                if ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    radar.zoom(1)
                if ev.key == pygame.K_s:
                    radar.settings.open_menu()
                if ev.key == pygame.K_h:
                    radar.recenter()
                if ev.key == pygame.K_m and radar.map:
                    radar.show_map = not radar.show_map
                if ev.key == pygame.K_f:
                    fullscreen = not fullscreen
                    screen = make_display(cfg, fullscreen)
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                radar.press(*to_canvas(*ev.pos))
            if ev.type == pygame.MOUSEMOTION and ev.buttons[0]:
                radar.drag(*to_canvas(*ev.pos))
            if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                radar.release(*to_canvas(*ev.pos))
            if ev.type == pygame.MOUSEWHEEL:
                radar.zoom(-1 if ev.y > 0 else 1)

        canvas = radar.draw(feed, time.time())
        if rot:
            canvas = pygame.transform.rotate(canvas, -rot)   # pygame turns anticlockwise, so negate
        screen.fill(BLACK)
        if side == CANVAS:
            screen.blit(canvas, (ox, oy))
        else:
            screen.blit(pygame.transform.smoothscale(canvas, (side, side)), (ox, oy))
        pygame.display.flip()
        clock.tick(cfg.get("fps", 30))
        if time.time() - last_beat > 10:                   # "still alive" for run.py's watchdog
            last_beat = time.time()
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(last_beat))
            except OSError:
                pass


if __name__ == "__main__":
    main()
