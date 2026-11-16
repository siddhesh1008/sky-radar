# Sky Radar: IKEA Skådis mount

A round enclosure for the Waveshare 5" round display, with a Raspberry Pi 5 and its active cooler
inside. It hangs on an IKEA Skådis pegboard. The display sits flush in the front, the Pi hangs behind it
from the back lid, and the USB-C power cable leaves through a slot in the bottom.

Outside size: Ø158 mm, 46 mm deep (57.6 mm including the Skådis hooks).

## Files

| File | What it is |
|---|---|
| `sky-radar-mount-shell.stl` | Front shell (holds the display) |
| `sky-radar-mount-lid-skadis.stl` | Back lid with Skådis hooks, Pi mounting holes and fan vents |
| `sky-radar-mount.f3d` | Fusion 360 file to edit the design |
| `sky-radar-mount.step` | STEP for other CAD programs |

The `.f3d` and `.step` also contain simple grey stand-ins named `REF …` (display, Pi 5, cooler)
for checking the fit. They are not printed.

## Printing (tested settings for a Bambu Lab printer)

- **Material:** PETG is recommended, since the Pi runs warm inside. PLA works if the room stays cool.
- 0.2 mm layers, 4 walls, 15–20 % infill.
- **Shell:** print **front face down** (it is exported in this orientation).
  Supports: **tree supports, build plate only**, for the four display-screw arms and the lid screw bosses.
- **Lid:** print **inner face down, hooks pointing up** (exported this way). No supports needed.

## Hardware

| Qty | Part | Used for |
|---|---|---|
| 4 | M3 × 6 mm screws | Display → shell (into the display's 75 × 75 mm threaded bosses) |
| 3 | M3 × 10 mm screws | Lid → shell (they cut their own thread in the plastic) |
| 4 | M2.5 × 20 mm female–female standoffs | Pi 5 → lid |
| 8 | M2.5 × 5 mm screws | Both ends of the standoffs |
| 1 | Short micro-HDMI → HDMI cable (15–20 cm, flexible) | Pi → display |
| 1 | Short USB-A → USB-C cable | Pi → display touch |
| 1 | USB-C power cable (≤ 7 mm thick) | Power, out through the bottom slot |

## Assembly

1. Push the display into the front of the shell until it rests on the inner ledge. Screw it in from
   behind with the 4 × M3 × 6 screws through the arms.
2. Screw the four standoffs onto the lid (screws from the outside of the lid).
3. Mount the Pi 5 **upside down** on the standoffs, with the cooler facing the lid. The fan then pulls
   air in through the slots right above it.
4. Connect HDMI and touch USB between the Pi and the display. Plug the USB-C power cable into the Pi
   and lay the cable into the slot in the bottom of the shell.
5. Close the lid with the 3 × M3 × 10 screws and hang it on the Skådis board: put the top hooks
   through the slots, then let it drop down onto the bottom pegs.

## Screen orientation

The shell holds the display with its connectors (HDMI, USB-C) pointing **down**, so the cables run to
the bottom where the power slot is. The display's screw pattern only fits this one way round.

The Waveshare display's picture is upright when its connectors point **up**, so the picture has to be
**rotated 180°** on the Pi. Touch rotates along with it:

- With the desktop: **Preferences → Screen Configuration**, right-click the screen →
  **Orientation → Inverted** → Apply.
- Or put this in `~/.config/kanshi/config` and reboot:
  ```
  profile {
  	output HDMI-A-1 enable mode 1080x1080@60.069 position 0,0 transform 180
  }
  ```

## Before printing, check
- The display's mounting bosses are assumed to be **M3**. Try an M3 screw in one before printing.

Display dimensions are taken from Waveshare's official 3D model (5inch 1080x1080 LCD, SKU 24369).
