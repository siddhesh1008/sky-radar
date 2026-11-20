"""Add a mic port to the Sky Radar shell: a 7-hole grille through the side wall + two zip-tie anchors inside.

Usage (needs: pip install trimesh manifold3d shapely numpy):
    python add_mic_port.py ..  ..
reads ../sky-radar-mount-shell.stl, writes ../sky-radar-mount-shell-mic.stl
"""
import sys, numpy as np, trimesh
from trimesh.creation import cylinder
H = sys.argv[1]
shell = trimesh.load(f"{H}/sky-radar-mount-shell.stl")
R_IN, R_OUT = 75.4, 79.0
ANG = 180.0                      # side wall, on the right when looking at the screen
ZC = 22.0                        # grille centre depth (display ends ~z 12, lid at z 46)
HOLE_D, PITCH = 2.0, 3.2

def to_angle(m):                 # parts are modelled at angle 180 (-x); rotate if ANG changes
    m.apply_transform(trimesh.transformations.rotation_matrix(np.radians(ANG - 180), [0, 0, 1])); return m

holes = []
pts = [(0, 0)] + [(PITCH * np.cos(a), PITCH * np.sin(a)) for a in np.radians(np.arange(0, 360, 60))]
for dy, dz in pts:
    c = cylinder(radius=HOLE_D / 2, height=12, sections=32)
    c.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))   # axis along x
    c.apply_translation([-(R_IN + R_OUT) / 2, dy, ZC + dz])
    holes.append(to_angle(c))

def anchor(yc):
    # profile in x-z, extruded along y: 45 deg chamfer underneath so it prints without supports
    x_wall, x_in, z0, z1, z2 = -76.5, -69.0, ZC - 4 - 7.5, ZC - 4, ZC + 4
    poly = np.array([[x_wall, z0], [x_in, z1], [x_in, z2], [x_wall, z2]])
    from shapely.geometry import Polygon
    blk = trimesh.creation.extrude_polygon(Polygon(poly), 5.0)       # extruded along its local z
    # map local (x, y=z_profile, z=extrude) -> world (x, y=extrude, z=profile)
    blk.apply_transform(np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float))
    blk.apply_translation([0, yc - 2.5, 0])
    slot = trimesh.creation.box(extents=[2.2, 12, 4.4])                 # tunnel for a <=4 mm zip tie
    slot.apply_translation([-71.6, yc, ZC])
    return to_angle(trimesh.boolean.difference([blk, slot]))

anchors = [anchor(-14.0), anchor(14.0)]

out = trimesh.boolean.union([shell, *anchors])
out = trimesh.boolean.difference([out, *holes])
print("watertight", out.is_watertight, "volume +%.2f cm3" % ((out.volume - shell.volume) / 1000), "bounds", np.round(out.bounds, 1).tolist())
out.export(f"{sys.argv[2]}/sky-radar-mount-shell-mic.stl")

