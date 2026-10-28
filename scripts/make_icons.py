"""Draws the app's icon, a crescent moon on a navy tile, into app/static: the browser tab's icon.svg and favicon.ico,
and the apple-touch-icon.png a phone shows for the app on its home screen.

Steps:
1. Work out the crescent: a circle with a smaller one cut out of its upper right.
2. Write the SVG.
3. Draw the PNGs pixel by pixel, smoothing the edges, and wrap the small ones in an ICO file.

Run it after changing the shape or the colors: python scripts/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
TILE_TOP, TILE_BOTTOM, MOON = (0x12, 0x34, 0x5C), (0x04, 0x12, 0x22), (0x6A, 0xA7, 0xF8)
SIZE, CORNER = 24, 6  # the drawing's units: a 24 x 24 tile with rounded corners
CENTER, RADIUS = (12.4, 11.6), 7.4
CUT_RADIUS, CUT_OFFSET = RADIUS * 0.8, RADIUS * 0.44  # the circle cut out, moved up and to the right


def crescent_path() -> tuple[str, tuple[float, float]]:
    """Returns the crescent as an SVG path, and the center of the circle cut out of it."""
    (cx, cy), r, d = CENTER, CUT_RADIUS, CUT_OFFSET
    ux, uy = math.cos(math.radians(-45)), math.sin(math.radians(-45))
    a = (RADIUS ** 2 - r ** 2 + d ** 2) / (2 * d)  # NOTE: where the two circles cross, along the line between centers
    h = math.sqrt(RADIUS ** 2 - a ** 2)
    mx, my = cx + a * ux, cy + a * uy
    (x1, y1), (x2, y2) = (mx - h * uy, my + h * ux), (mx + h * uy, my - h * ux)
    f = lambda v: f"{v:.3f}".rstrip("0").rstrip(".")
    # From one tip the long way round the moon's outside to the other, then back along the cut's edge.
    path = f"M{f(x2)} {f(y2)}A{f(RADIUS)} {f(RADIUS)} 0 1 0 {f(x1)} {f(y1)}A{f(r)} {f(r)} 0 1 1 {f(x2)} {f(y2)}Z"
    return path, (cx + d * ux, cy + d * uy)


def svg() -> str:
    """Returns the icon as the text of an SVG file."""
    hexes = ["#" + bytes(c).hex() for c in (TILE_TOP, TILE_BOTTOM, MOON)]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">'
            f'<defs><linearGradient id="tile" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{hexes[0]}"/>'
            f'<stop offset="1" stop-color="{hexes[1]}"/></linearGradient></defs>'
            f'<rect width="{SIZE}" height="{SIZE}" rx="{CORNER}" fill="url(#tile)"/>'
            f'<path d="{crescent_path()[0]}" fill="{hexes[2]}"/></svg>\n')


def png(pixels: int, corner: float, samples: int = 4) -> bytes:
    """Returns the icon drawn `pixels` wide as a PNG, with corners of `corner` units (0 for square ones)."""
    cut = crescent_path()[1]
    rows = []
    for py in range(pixels):
        row = bytearray([0])  # NOTE: each PNG row starts with its filter type, 0 for none
        for px in range(pixels):
            r = g = b = a = 0.0
            for sy in range(samples):
                for sx in range(samples):
                    x, y = ((px + (sx + 0.5) / samples) * SIZE / pixels, (py + (sy + 0.5) / samples) * SIZE / pixels)
                    # Past both straight edges near a corner, the point is outside once it's beyond the corner's curve
                    dx, dy = max(corner - x, x - (SIZE - corner), 0), max(corner - y, y - (SIZE - corner), 0)
                    if corner and dx * dx + dy * dy > corner * corner:
                        continue
                    if math.dist((x, y), CENTER) <= RADIUS and math.dist((x, y), cut) > CUT_RADIUS:
                        color = MOON
                    else:
                        t = y / SIZE
                        color = tuple(top + (bottom - top) * t for top, bottom in zip(TILE_TOP, TILE_BOTTOM))
                    r, g, b, a = r + color[0], g + color[1], b + color[2], a + 1
            n = samples * samples
            row += bytes([round(r / a), round(g / a), round(b / a), round(255 * a / n)] if a else [0, 0, 0, 0])
        rows.append(bytes(row))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", pixels, pixels, 8, 6, 0, 0, 0)  # 8 bits per channel, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def ico(images: dict[int, bytes]) -> bytes:
    """Returns an ICO file holding the PNGs in `images`, keyed by width."""
    out, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for width, data in images.items():
        out += struct.pack("<BBBBHHII", width, width, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images.values())


if __name__ == "__main__":
    (STATIC / "icon.svg").write_text(svg())
    (STATIC / "favicon.ico").write_bytes(ico({size: png(size, CORNER) for size in (16, 32, 64)}))
    (STATIC / "apple-touch-icon.png").write_bytes(png(180, 0))  # NOTE: the phone rounds the corners itself
    print("Wrote icon.svg, favicon.ico and apple-touch-icon.png in", STATIC)
