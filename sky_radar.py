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
import queue
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
SEARCH_URL = "https://photon.komoot.io/api/"

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


# --------------------------------------------------------------------------- settings UI
def draw_button(ui, rect, label, primary=False, size=28):
    cv = ui.canvas
    pygame.draw.rect(cv, SCOPE_BG, rect, border_radius=10)
    pygame.draw.rect(cv, SWEEP if primary else RING, rect, 2 if primary else 1, border_radius=10)
    ui.blit_text(label, size, SWEEP if primary else TEXT, rect.center)


def draw_screen_bg(ui, title, subtitle=""):
    cv = ui.canvas
    cv.fill(BLACK)
    pygame.draw.circle(cv, (2, 20, 17), (C, C), C)
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


# --------------------------------------------------------------------------- settings menu
class Settings:
    """Settings menu (gear button) and the first-run setup: Wi-Fi, then location."""

    def __init__(self, ui, location, cfg):
        self.ui = ui
        self.location = location
        self.cfg = cfg
        self.wifi = FakeWifi() if os.environ.get("SKY_RADAR_FAKE_WIFI") else NmcliWifi()
        self.screens = {"location": LocationScreen(self), "wifi": WifiScreen(self)}
        self.screen = None            # None = radar, "menu", "location" or "wifi"
        self.flow = []                # remaining first-run steps
        self.from_menu = False
        self.rects = []
        self.wifi_name = None
        self._pending_flow = None
        threading.Thread(target=self._first_run_check, daemon=True).start()

    @property
    def active(self):
        return self.screen is not None

    # -- navigation ---------------------------------------------------------------
    def open_menu(self):
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
        rows = [("Location", self.location.name, lambda: self.show("location")),
                ("Wi-Fi", wifi_value, lambda: self.show("wifi")),
                ("Pinch to zoom", touch_value, self._toggle_pinch)]
        y = 230
        for title, value, action in rows:
            rect = pygame.Rect(0, 0, 700, 100)
            rect.midtop = (C, y)
            pygame.draw.rect(ui.canvas, SCOPE_BG, rect, border_radius=14)
            pygame.draw.rect(ui.canvas, RING, rect, 1, border_radius=14)
            ui.blit_text(title, 34, TEXT, (rect.x + 28, rect.y + 22), "topleft")
            ui.blit_text(value[:44], 28, TEXT_DIM, (rect.x + 28, rect.y + 62), "topleft")
            if title == "Pinch to zoom" and touch is not None:
                self._draw_switch(ui, rect.right - 70, rect.centery, touch)
            elif title != "Pinch to zoom":
                ui.blit_text(">", 44, SWEEP, (rect.right - 40, rect.centery))
            self.rects.append((rect, action))
            y += 120
        close = pygame.Rect(0, 0, 260, 70)
        close.midtop = (C, 610)
        draw_button(ui, close, "Close", primary=True)
        self.rects.append((close, self.close))


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
        self.view = None          # set by the radar: () -> (lat, lon, radius_nm) of what is on screen
        threading.Thread(target=self._run, daemon=True).start()

    def _url(self):
        if self.cfg["source"] == "local":
            return self.cfg["local_url"]
        # Planes around the part of the map on screen. Only a rounded point (~10 km) is sent.
        if self.view:
            lat, lon, radius = self.view()
        else:
            (lat, lon), radius = self.location.current(), self.cfg["fetch_radius_nm"]
        return self.cfg["api_url"].format(lat=round(lat, 1), lon=round(lon, 1), radius_nm=int(radius))

    def _run(self):
        while True:
            try:
                req = urllib.request.Request(self._url(), headers={"User-Agent": "sky-radar/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.load(resp)
                planes = data.get("ac") or data.get("aircraft") or []
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
                    self.status = "Live: " + ("own receiver" if self.cfg["source"] == "local" else "adsb.lol")
                    self.last_ok = time.time()
            except Exception as e:  # network hiccups should never kill the display
                with self.lock:
                    self.status = f"Feed error: {type(e).__name__}"
            self.wake.wait(self.cfg["poll_seconds"])   # wakes early when the location changes
            self.wake.clear()

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
        night.fill((110, 190, 170), special_flags=pygame.BLEND_RGB_MULT)
        night.set_alpha(self.alpha)
        final = pygame.Surface((size, size))   # opaque, so drawing it each frame is cheap
        final.fill(SCOPE_BG)
        final.blit(night, (0, 0))
        return final, missing


def fmt_dist(km):
    """1.5 -> '1.5 km', 0.05 -> '50 m'."""
    if km < 1:
        return f"{km * 1000:g} m"
    return f"{km:g} km"


def is_military(p):
    return bool(p.get("dbFlags", 0) & 1)


def callsign(p):
    return (p.get("flight") or "").strip() or p.get("r") or p.get("hex", "?").upper()


def fmt_alt(p):
    alt = p.get("alt_baro", p.get("alt_geom"))
    if alt == "ground":
        return "ground"
    if isinstance(alt, (int, float)):
        return f"{int(alt):,} ft"
    return "? ft"


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


# --------------------------------------------------------------------------- radar view
class Radar:
    def __init__(self, cfg, location):
        self.cfg = cfg
        self.location = location
        self.home_lat, self.home_lon = location.current()
        self.view_lat, self.view_lon = self.home_lat, self.home_lon   # centre of the screen
        self._update_view()
        location.listeners.append(self.recenter)                       # new location chosen: jump there
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
        self.settings = Settings(self, location, cfg)
        self.info = AircraftInfo()
        self.card_rect = None
        self.card_collapsed = False
        self._outside = pygame.Surface((CANVAS, CANVAS))   # black everywhere except the round screen
        self._outside.fill(BLACK)
        pygame.draw.circle(self._outside, (255, 0, 255), (C, C), C)
        self._outside.set_colorkey((255, 0, 255))
        self._halo = pygame.Surface((64, 64), pygame.SRCALPHA)       # soft halo around the blue dot
        pygame.draw.circle(self._halo, (*HOME_BLUE, 60), (32, 32), 30)

    @property
    def range_km(self):
        return self.live_range or self.ranges[self.range_idx]

    def zoom(self, step):
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

    def press(self, x, y):
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
        if self._press is not None and not self.dragging:
            self.tap(x, y)
        self._press = None
        self.dragging = False

    def text(self, s, size, color):
        key = (s, size, color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 600:
                self._text_cache.clear()
            surf = self._text_cache[key] = self.fonts[size].render(s, True, color)
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
        if self.selected and self.card_rect and self.card_rect.collidepoint(x, y):
            self.card_collapsed = not self.card_collapsed   # tap the card: shrink it to see the path
            return
        best, best_d = None, 45
        for hx, (px, py) in self.screen_pos.items():
            d = math.hypot(x - px, y - py)
            if d < best_d:
                best, best_d = hx, d
        if best != self.selected:
            self.card_collapsed = False
        self.selected = best

    # -- drawing ------------------------------------------------------------
    def draw(self, feed, now):
        cv = self.canvas
        self.settings.update(now)
        if self.settings.active:
            self.settings.draw(now)
            return cv
        aircraft, mil_no_pos, trails, status, last_ok = feed.snapshot()
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
        in_range, mil_count, overhead = [], 0, None
        for hx, p in aircraft.items():
            hx_, hy_ = self.home_offset_km(p["lat"], p["lon"])
            p["_dist_km"] = math.hypot(hx_, hy_)            # distance from you (the blue dot)
            if p["_dist_km"] <= self.cfg["overhead_alert_km"] and (overhead is None or p["_dist_km"] < overhead["_dist_km"]):
                overhead = p
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
        cv.blit(self._outside, (0, 0))    # trim map, paths and labels to the round screen

        if overhead:
            self._draw_overhead(overhead, now)

        # HUD
        hud_bg = (2, 20, 17)
        place = self.location.name
        self.blit_text(f"{place}  {time.strftime('%H:%M')}", 34, TEXT, (C, 100), bg=hud_bg)
        summary = f"{len(in_range)} aircraft  |  {mil_count} military  |  {fmt_dist(self.range_km)}"
        self.blit_text(summary, 34, TEXT, (C, 930), bg=hud_bg)
        if mil_no_pos:
            self.blit_text(f"+{len(mil_no_pos)} military heard, no position", 28, MIL, (C, 893), bg=hud_bg)
        stale = last_ok and now - last_ok > 30
        self.blit_text(status + ("  (stale)" if stale else ""), 24, WARN if (stale or "error" in status) else TEXT_DIM, (C, 966), bg=hud_bg)
        if base:
            self.blit_text("Map (c) OpenStreetMap contributors", 24, TEXT_DIM, (C, 1000))
        elif self.show_map:
            self.blit_text("Loading map...", 24, TEXT_DIM, (C, 1000))

        self._draw_buttons()
        self.card_rect = None
        if self.selected:
            self._draw_info(aircraft[self.selected], now)
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
        scale = 0.6 if on_ground else (1.4 if mil else 1.0)
        pts = [(x + (px * ct - py * st) * scale, y + (px * st + py * ct) * scale) for px, py in PLANE_SHAPE]
        pygame.draw.polygon(self.canvas, col, pts)
        if on_ground and hx != self.selected:
            return            # parked/taxiing aircraft: icon only, no clutter
        if x > C + 250:   # near the right edge of the round screen: label on the left
            self.blit_text(callsign(p), 28, col, (x - 20, y - 18), "topright")
            self.blit_text(fmt_alt(p), 24, col, (x - 20, y + 2), "topright")
        else:
            self.blit_text(callsign(p), 28, col, (x + 20, y - 18), "topleft")
            self.blit_text(fmt_alt(p), 24, col, (x + 20, y + 2), "topleft")

    def _draw_overhead(self, p, now):
        pulse = (now * 1.5) % 1.0
        col = MIL if is_military(p) else SELECT
        pygame.draw.circle(self.canvas, col, (C, C), int(20 + 60 * pulse), 3)
        tag = "MILITARY OVERHEAD" if is_military(p) else "OVERHEAD"
        self.blit_text(f"{tag}: {callsign(p)}  {fmt_alt(p)}", 44, col, (C, 160))

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
            arrive = time.strftime("%H:%M", time.localtime(now + hours * 3600))
            return (f"Arrives {dest['code']}", arrive, "in " + fmt_duration(hours))
        # no destination (military): when is it closest to you?
        dx, dy = self.home_offset_km(p["lat"], p["lon"])
        t = math.radians(p.get("track") or 0)
        vx, vy = speed_kmh * math.sin(t), speed_kmh * math.cos(t)
        t_cpa = -(dx * vx + dy * vy) / (speed_kmh ** 2)
        if t_cpa <= 0:
            return ("Closest to you", "passed", "moving away")
        miss = math.hypot(dx + vx * t_cpa, dy + vy * t_cpa)
        return ("Closest to you", "in " + fmt_duration(t_cpa), f"{fmt_dist(round(miss, 1 if miss >= 1 else 2))} away")

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
            rect = self.text(label, 24, col).get_rect(center=(C, 160 if at_top else 845)).inflate(28, 18)
            self.card_rect = rect
            pygame.draw.rect(cv, (2, 20, 17), rect, border_radius=12)
            pygame.draw.rect(cv, col, rect, 2, border_radius=12)
            self.blit_text(label, 24, col, rect.center)
            return

        loading = info["state"] == "loading"
        rect = pygame.Rect(0, 0, 480, 140 if mil else 196)
        if at_top:
            rect.midtop = (C, 135)
        else:
            rect.midbottom = (C, 878)
        self.card_rect = rect
        pygame.draw.rect(cv, (2, 20, 17), rect, border_radius=14)
        pygame.draw.rect(cv, col, rect, 2, border_radius=14)
        x0, x1, y = rect.x + 18, rect.right - 18, rect.y + 12

        # model + military badge
        model = info["model"] or ("Looking up..." if loading else ("Type not broadcast" if mil else "Unknown type"))
        self.blit_text(model[:26], 34, col, (x0, y), "topleft")
        if mil:
            badge = self.text("MILITARY", 20, (2, 20, 17)).get_rect(topright=(x1, y + 4))
            pygame.draw.rect(cv, MIL, badge.inflate(12, 6), border_radius=6)
            cv.blit(self.text("MILITARY", 20, (2, 20, 17)), badge)
        y += 32

        # operator / airline + callsign
        who = info["operator"] or ("" if loading else ("Unknown operator" if mil else "Unknown airline"))
        self.blit_text("  |  ".join(x for x in (who[:30], callsign(p)) if x), 24, TEXT, (x0, y), "topleft")
        y += 28

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

    while True:
        sw, sh = screen.get_size()
        side = min(sw, sh)
        scale = side / CANVAS
        ox, oy = (sw - side) // 2, (sh - side) // 2

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.FINGERUP:            # always, so no finger is left "stuck down"
                radar.finger_up(ev.finger_id)
            elif ev.type in (pygame.FINGERDOWN, pygame.FINGERMOTION) and not radar.settings.active:
                fx, fy = (ev.x * sw - ox) / scale, (ev.y * sh - oy) / scale   # touch is 0..1 of the window
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
                    radar.tap((ev.pos[0] - ox) / scale, (ev.pos[1] - oy) / scale)
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
                radar.press((ev.pos[0] - ox) / scale, (ev.pos[1] - oy) / scale)
            if ev.type == pygame.MOUSEMOTION and ev.buttons[0]:
                radar.drag((ev.pos[0] - ox) / scale, (ev.pos[1] - oy) / scale)
            if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                radar.release((ev.pos[0] - ox) / scale, (ev.pos[1] - oy) / scale)
            if ev.type == pygame.MOUSEWHEEL:
                radar.zoom(-1 if ev.y > 0 else 1)

        canvas = radar.draw(feed, time.time())
        screen.fill(BLACK)
        if side == CANVAS:
            screen.blit(canvas, (ox, oy))
        else:
            screen.blit(pygame.transform.smoothscale(canvas, (side, side)), (ox, oy))
        pygame.display.flip()
        clock.tick(cfg.get("fps", 30))


if __name__ == "__main__":
    main()
