#!/usr/bin/env python3
"""Sky Radar: one file to set up and start, on Windows, Raspberry Pi / Linux and macOS.

    Windows:        double-click run.py   (or: py run.py)
    Pi / Linux:     python3 run.py

It checks which system it is on and then:
  1. creates a private Python environment in .venv (first run only)
  2. installs the packages from requirements.txt (only when that file changed)
     - on a Pi / Debian it installs missing system packages with apt if needed
  3. on a Raspberry Pi: adds Sky Radar to the desktop autostart, so it starts at boot,
     and turns off touch "mouse emulation" so pinch-to-zoom works
  4. starts Sky Radar (fullscreen on a Raspberry Pi)

Options:
    --setup-only            set everything up, but don't start the app
    --windowed              start in a window, even on a Pi
    --no-autostart          don't add Sky Radar to the Pi's autostart
    --keep-touch-emulation  leave the Pi's touch settings as they are (pinch-to-zoom won't work)
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
STAMP = os.path.join(VENV, ".requirements.sha256")
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")


def say(msg):
    print(f"[sky-radar] {msg}", flush=True)


def fail(msg):
    say(msg)
    if IS_WINDOWS:
        input("Press Enter to close this window.")   # double-clicked: keep the message visible
    sys.exit(1)


def is_raspberry_pi():
    try:
        with open("/proc/device-tree/model", errors="ignore") as f:
            return "raspberry pi" in f.read().lower()
    except OSError:
        return False


def apt_install(*packages):
    """Install Debian / Raspberry Pi OS packages. Returns True on success."""
    if not IS_LINUX or not shutil.which("apt-get"):
        return False
    say(f"Installing {' '.join(packages)} with apt (it may ask for your password) ...")
    return subprocess.call(["sudo", "apt-get", "install", "-y", *packages]) == 0


def venv_python():
    if IS_WINDOWS:
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def ensure_venv():
    if os.path.exists(venv_python()):
        return
    say("Creating a virtual environment in .venv ...")
    # On Linux the venv can also see system packages, so the Pi's python3-pygame
    # package works as a fallback if pip can't install pygame-ce.
    builder = venv.EnvBuilder(with_pip=True, system_site_packages=IS_LINUX)
    try:
        builder.create(VENV)
    except Exception:
        shutil.rmtree(VENV, ignore_errors=True)
        if apt_install("python3-venv"):                 # Debian ships venv separately
            builder.create(VENV)
        else:
            fail("Couldn't create the virtual environment. On Raspberry Pi / Debian run: "
                 "sudo apt install python3-venv")


def pygame_works():
    return subprocess.call([venv_python(), "-c", "import pygame"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def ensure_packages():
    with open(REQUIREMENTS, "rb") as f:
        wanted = hashlib.sha256(f.read()).hexdigest()
    try:
        with open(STAMP) as f:
            if f.read().strip() == wanted and pygame_works():
                say("Packages are up to date.")
                return
    except OSError:
        pass
    py = venv_python()
    say("Installing packages (the first time can take a minute) ...")
    subprocess.call([py, "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    if subprocess.call([py, "-m", "pip", "install", "-r", REQUIREMENTS]) != 0 and not pygame_works():
        # e.g. 32-bit Raspberry Pi OS, where there is no ready-made pygame-ce: use the system package
        if not (apt_install("python3-pygame") and pygame_works()):
            fail("Couldn't install pygame. Check the internet connection and run this again.")
        say("Using the system pygame package instead of pygame-ce.")
    with open(STAMP, "w") as f:
        f.write(wanted)


def ensure_pi_autostart():
    """Start Sky Radar when the Pi's desktop starts (Raspberry Pi OS Bookworm, labwc)."""
    path = os.path.expanduser("~/.config/labwc/autostart")
    line = f'python3 "{os.path.join(HERE, "run.py")}" &'
    try:
        with open(path) as f:
            if line in f.read():
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")
    say(f"Added Sky Radar to {path}, so it starts at boot. Delete that line to stop it.")


def ensure_pi_multitouch():
    """Raspberry Pi OS sets touch screens to "mouse emulation", which turns every touch into one mouse
    pointer, so a second finger never reaches the app and pinch-to-zoom can't work. Switch it off.
    (Pi OS's autotouch tool won't switch it back: it only writes this setting when there is none yet.)"""
    try:
        with open(os.path.join(HERE, "prefs.json")) as f:
            if json.load(f).get("multitouch") is False:
                return    # you switched pinch-to-zoom off in Settings: keep it that way
    except (OSError, ValueError):
        pass
    path = os.path.expanduser("~/.config/labwc/rc.xml")
    try:
        with open(path) as f:
            xml = f.read()
    except OSError:
        return            # not created yet (autotouch writes it at the first desktop login); next start fixes it
    if 'mouseEmulation="yes"' not in xml:
        return
    backup = path + ".before-sky-radar"
    if not os.path.exists(backup):
        shutil.copyfile(path, backup)
    with open(path, "w") as f:
        f.write(xml.replace('mouseEmulation="yes"', 'mouseEmulation="no"'))
    say(f"Turned off touch mouse emulation in {path} so pinch-to-zoom works (backup: {backup}).")
    subprocess.call(["pkill", "-HUP", "-x", "labwc"])   # make the desktop reload its settings now


def main():
    args = sys.argv[1:]
    setup_only = "--setup-only" in args
    windowed = "--windowed" in args
    autostart = "--no-autostart" not in args
    keep_touch = "--keep-touch-emulation" in args
    args = [a for a in args if a not in ("--setup-only", "--windowed", "--no-autostart", "--keep-touch-emulation")]
    pi = is_raspberry_pi()

    say(f"System: {'Raspberry Pi' if pi else platform.system()}, Python {platform.python_version()}")
    if sys.version_info < (3, 9):
        fail("Python 3.9 or newer is needed. Get it from https://www.python.org/downloads/")

    ensure_venv()
    ensure_packages()
    if pi and autostart:
        ensure_pi_autostart()
    if pi and not keep_touch:
        ensure_pi_multitouch()
    if setup_only:
        say("Setup complete.")
        return

    if pi and not windowed and "--fullscreen" not in args:
        args.append("--fullscreen")
    env = os.environ.copy()
    if IS_LINUX and env.get("WAYLAND_DISPLAY") and "SDL_VIDEODRIVER" not in env:
        # Native Wayland (not the X11 layer): needed to receive real multi-finger touch for pinch-to-zoom
        env["SDL_VIDEODRIVER"] = "wayland"
    say("Starting Sky Radar ...")
    code = subprocess.call([venv_python(), os.path.join(HERE, "sky_radar.py")] + args, env=env)
    if code != 0:
        fail(f"Sky Radar stopped with an error (code {code}).")


if __name__ == "__main__":
    main()
